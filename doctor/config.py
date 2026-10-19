"""Thresholds and runtime settings.

LIMITS come from listening tests and Whisper/ECAPA measurements of OmniVoice + the Ukrainian LoRA
(dkaraush/omnivoice-lora-uk) with accentuk-stressed text. Each pair is (warn, bad).
"""
import json
import os
import threading

SR = 24000  # OmniVoice works at 24 kHz

LIMITS = {
    "speech_s_min": (6.0, 4.0),       # speech after silence removal; 3 s: voice similarity drops sharply
    "speech_s_max": (15.0, 20.0),     # OmniVoice trims prompts over 20 s
    "text_wer": (0.05, 0.3),          # transcript vs Whisper; a wrong transcript is the main cause of
                                      # swallowed phrases, ad-libs and sample leaks into the output
    "snr_db": (30.0, 22.0),           # below 22 dB the noise is always heard in the output
    "clip_frac": (0.0005, 0.005),     # share of clipped samples
    "rolloff_hz": (6000.0, 3500.0),   # bandwidth; mp3 at 32 kbit/s (6.5-7.7 kHz) costs little
    "pause_max_s": (2.0, 4.0),        # long pauses are harmless: OmniVoice keeps 0.2 s of each
    "rate_min": (9.0, 7.5),           # text weight per second of speech: slow samples give slow output
    "rate_max": (14.0, 17.0),         # fast samples make the length estimate too short: swallowed words
    "trail_sil_min": (0.1, -1.0),     # sample cut right after the last word: a sound at the output start
}
TARGET_SPEECH_S = (6.0, 12.0)         # crop window used by repair
TARGET_RMS_DBFS = -20.0
LEAD_SIL_S, TRAIL_SIL_S, PAUSE_CAP_S = 0.1, 0.2, 0.3
# Whisper errs on slang, names and profanity: a given transcript is kept unless it is clearly another text
REPLACE_TEXT_WER = LIMITS["text_wer"][1]

ASR_MODELS = {  # name -> rough memory need in MB (fp16 on GPU / fp32 on CPU)
    "openai/whisper-large-v3": (3600, 6800),
    "openai/whisper-large-v3-turbo": (1900, 3500),
    "openai/whisper-small": (600, 1100),
}
SIDON_REPO = "sarulab-speech/sidon-v0.1"
SIDON_MB = (2200, 2400)

# .info fields filled in when the uploaded voice has none: OmniVoice generation defaults
# (cfg_strength = guidance_scale 2.0, speed 1.0); the name defaults to the file name
INFO_DEFAULTS = {"cfg_strength": 2.0, "speed": 1.0}

DEFAULTS = {
    "device": "auto",          # auto | cuda | cpu
    "asr_model": "openai/whisper-large-v3",
    "idle_unload_s": 300,      # unload a model after this many idle seconds
    "reserve_mb": 1024,        # keep this much memory free when loading a model
}


class Settings:
    """Small JSON-backed settings store."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = dict(DEFAULTS)
        if os.path.exists(path):
            self.data.update(json.load(open(path, encoding="utf-8")))

    def get(self, key):
        return self.data[key]

    def update(self, **kw):
        with self.lock:
            for k, v in kw.items():
                if k in DEFAULTS:
                    self.data[k] = type(DEFAULTS[k])(v)
            json.dump(self.data, open(self.path, "w", encoding="utf-8"), indent=1)
        return dict(self.data)
