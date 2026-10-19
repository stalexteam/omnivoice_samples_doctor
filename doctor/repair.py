"""Sample repair: transcript choice, crop at a word boundary, optional Sidon denoise, edge silences,
pause cap, loudness."""
import jiwer
import numpy as np

from .analysis import norm_words, speech_intervals
from .config import (LEAD_SIL_S, LIMITS, PAUSE_CAP_S, REPLACE_TEXT_WER, SR, TARGET_RMS_DBFS, TARGET_SPEECH_S,
                     TRAIL_SIL_S)


def _cut_user_text(user_text, asr_words, n_keep):
    """Prefix of the user's transcript that matches the first n_keep ASR words (keeps the user's spelling)."""
    raw = user_text.split()
    idx = [i for i, t in enumerate(raw) if norm_words(t)]  # raw tokens that carry words
    ref = [norm_words(raw[i]).split()[0] for i in idx]
    hyp = [norm_words(w["text"]) or "_" for w in asr_words]
    last_ref = -1
    for c in jiwer.process_words(" ".join(ref), " ".join(h.split()[0] for h in hyp)).alignments[0]:
        if c.type in ("equal", "substitute"):
            for k in range(c.hyp_end_idx - c.hyp_start_idx):
                if c.hyp_start_idx + k < n_keep:
                    last_ref = c.ref_start_idx + k
    return " ".join(raw[: idx[last_ref] + 1]) if last_ref >= 0 else None


def repair(y, text, r, words, denoise=None, denoise_mode="auto"):
    """y: mono float at SR; r: analysis of y; words: ASR words [{"text", "start", "end"}].
    denoise: callable(y) -> y, may raise (e.g. NotEnoughMemory); the step is then skipped.
    Returns (y, text, notes) where notes are (code, value) pairs for the UI."""
    notes = []
    asr_text = r.get("asr_text")
    if r.get("asr_loop"):  # Whisper got stuck: its text and word times are unusable
        if not text:
            raise ValueError("speech recognition failed on this sample: enter the transcript by hand")
        asr_text, words = None, None
        notes.append(("asr_loop", None))
    use_text = text
    if not text:
        if not asr_text:
            raise ValueError("no transcript: give one or run transcription first")
        use_text = asr_text
        notes.append(("text_from_asr", None))
    elif asr_text and (r.get("text_wer") or 0) > REPLACE_TEXT_WER:
        use_text = asr_text
        notes.append(("text_replaced", r["text_wer"]))
    elif (r.get("text_wer") or 0) > LIMITS["text_wer"][0]:
        notes.append(("text_kept_check", r["text_wer"]))

    # crop to the target length, preferring a clause end
    if r["speech_s"] > TARGET_SPEECH_S[1]:
        if words:
            lim = TARGET_SPEECH_S[1] + r["lead_sil_s"]
            keep = [w for w in words if w["end"] is not None and w["end"] <= lim]
            for i in range(len(keep) - 1, 0, -1):
                if keep[i]["text"].strip()[-1:] in ".!?,;…" and keep[i]["end"] >= TARGET_SPEECH_S[0]:
                    keep = keep[: i + 1]
                    break
            if keep:
                y = y[: int((keep[-1]["end"] + 0.1) * SR)]
                cut = _cut_user_text(use_text, words, len(keep)) if use_text != asr_text else None
                use_text = cut or "".join(w["text"] for w in keep).strip()
                notes.append(("cropped", round(keep[-1]["end"], 1)))
        else:
            notes.append(("not_cropped_no_asr", r["speech_s"]))

    # denoise after the crop: Sidon then sees at most ~13 s
    want = denoise_mode == "on" or (denoise_mode == "auto" and (r.get("snr_db") or 99) < LIMITS["snr_db"][1])
    if want and denoise is not None:
        try:
            y = denoise(y)
            notes.append(("denoised", r.get("snr_db")))
        except Exception as e:  # not enough memory or a model failure: keep going without this step
            notes.append(("denoise_skipped", str(e)))

    # edges: 0.1 s before, 0.2 s of clean silence after; internal pauses capped
    iv = speech_intervals(y)
    pieces = []
    for i, (a, b) in enumerate(iv):
        pieces.append(y[a:b])
        if i < len(iv) - 1:
            pieces.append(y[b: b + min(iv[i + 1][0] - b, int(PAUSE_CAP_S * SR))])
    speech = np.concatenate(pieces)
    y = np.concatenate([np.zeros(int(LEAD_SIL_S * SR)), speech, np.zeros(int(TRAIL_SIL_S * SR))])
    notes.append(("edges_pauses", PAUSE_CAP_S))

    rms = np.sqrt(np.mean(speech ** 2)) + 1e-9
    y = y * (10 ** (TARGET_RMS_DBFS / 20) / rms)
    peak = np.max(np.abs(y))
    if peak > 0.97:
        y = y * 0.97 / peak
    notes.append(("loudness", TARGET_RMS_DBFS))
    return y.astype(np.float32), use_text, notes
