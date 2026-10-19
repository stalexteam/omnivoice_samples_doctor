"""Sample analysis: length, silences, loudness, noise, bandwidth, transcript match, speaking rate."""
import re

import jiwer
import librosa
import numpy as np

from .config import LIMITS, SR


def text_weight(t):
    """OmniVoice duration weight (omnivoice.utils.duration.RuleDurationEstimator, Cyrillic subset).
    The model sets the output length from weight(text) / (weight(ref_text) / ref_speech_seconds)."""
    w = 0.0
    for ch in t:
        if ch.isspace():
            w += 0.2
        elif ch.isdigit():
            w += 3.5
        elif ch in ".,;:!?…—–-\"'«»()":
            w += 0.5
        elif ch == "́":  # stress mark
            continue
        else:
            w += 1.0
    return w


def norm_words(s):
    s = s.replace("́", "").replace("’", "'").replace("ʼ", "'").lower()
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s']", " ", s)).strip()


def speech_intervals(y, top_db=35):
    return librosa.effects.split(y, top_db=top_db, frame_length=1024, hop_length=256)


def analyse(y, text=None, words=None, asr_text=None):
    """Measure a mono 24 kHz signal. `asr_text` / `words` come from a prior transcription."""
    r = {"dur_s": round(len(y) / SR, 2)}
    iv = speech_intervals(y)
    if len(iv) == 0:
        return {**r, "error": "no speech found"}
    r["lead_sil_s"] = round(iv[0][0] / SR, 2)
    r["trail_sil_s"] = round((len(y) - iv[-1][1]) / SR, 2)
    gaps = [(iv[i + 1][0] - iv[i][1]) / SR for i in range(len(iv) - 1)]
    r["pause_max_s"] = round(max(gaps), 2) if gaps else 0.0
    # OmniVoice keeps at most 0.2 s of each internal pause when it measures the prompt
    speech = sum(b - a for a, b in iv) / SR + sum(min(g, 0.2) for g in gaps)
    r["speech_s"] = round(float(speech), 2)
    sp = np.concatenate([y[a:b] for a, b in iv])
    r["rms_dbfs"] = round(float(20 * np.log10(np.sqrt(np.mean(sp ** 2)) + 1e-9)), 1)
    r["peak_dbfs"] = round(float(20 * np.log10(np.max(np.abs(y)) + 1e-9)), 1)
    r["clip_frac"] = round(float(np.mean(np.abs(y) > 0.99)), 5)
    e = librosa.feature.rms(y=y, frame_length=1024, hop_length=256)[0]
    snr = 20 * np.log10((np.percentile(e, 95) + 1e-9) / (np.percentile(e, 10) + 1e-9))
    r["snr_db"] = round(float(min(snr, 99.0)), 1)  # digital silence gives absurd values
    roll = librosa.feature.spectral_rolloff(y=sp, sr=SR, roll_percent=0.99)[0]
    r["rolloff_hz"] = int(np.median(roll))
    if asr_text is not None:
        r["asr_text"] = asr_text
        if text:
            r["text_wer"] = round(float(jiwer.wer(norm_words(text), norm_words(asr_text) or "—")), 3)
    t = text or asr_text or ""
    r["rate"] = round(text_weight(t) / speech, 2) if t else None
    return r


def verdict(r, has_text):
    """Return ("OK" | "WARN" | "BAD", [(level, key, value, warn, bad)])."""
    issues = []

    def chk(key, val, lo_is_bad=False, lim=None):
        if val is None:
            return
        warn, bad = LIMITS[lim or key]
        if lo_is_bad:
            lvl = "BAD" if val < bad else "WARN" if val < warn else None
        else:
            lvl = "BAD" if val > bad else "WARN" if val > warn else None
        if lvl:
            issues.append((lvl, lim or key, val, warn, bad))

    if r.get("error"):
        return "BAD", [("BAD", "no_speech", None, None, None)]
    chk("speech_s", r.get("speech_s"), True, "speech_s_min")
    chk("speech_s", r.get("speech_s"), False, "speech_s_max")
    chk("snr_db", r.get("snr_db"), True)
    chk("clip_frac", r.get("clip_frac"))
    chk("rolloff_hz", r.get("rolloff_hz"), True)
    chk("pause_max_s", r.get("pause_max_s"))
    chk("rate", r.get("rate"), True, "rate_min")
    chk("rate", r.get("rate"), False, "rate_max")
    chk("trail_sil_s", r.get("trail_sil_s"), True, "trail_sil_min")
    if has_text:
        chk("text_wer", r.get("text_wer"))
    else:
        issues.append(("WARN", "no_text", None, None, None))
    level = "BAD" if any(i[0] == "BAD" for i in issues) else "WARN" if issues else "OK"
    return level, issues
