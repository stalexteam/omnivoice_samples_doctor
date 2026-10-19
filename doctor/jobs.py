"""Single background worker: jobs run one at a time, so at most one model works at once."""
import logging
import queue
import threading
import time
import traceback

import librosa

from . import sidon
from .analysis import analyse, verdict
from .config import SR
from .models import NotEnoughMemory
from .repair import repair

log = logging.getLogger("doctor")
ACTIONS = ("transcribe", "analyse", "repair")


class Worker:
    def __init__(self, store, models):
        self.store, self.models = store, models
        self.q = queue.Queue()
        threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, sid, action, **params):
        if action not in ACTIONS:
            raise ValueError(action)
        self.store.update(sid, job={"action": action, "state": "queued", "t": time.time(), "msg": None})
        self.q.put((sid, action, params))

    def pending(self):
        return self.q.qsize()

    def _loop(self):
        while True:
            sid, action, params = self.q.get()
            try:
                self.store.update(sid, job={"action": action, "state": "running", "t": time.time(), "msg": None})
                getattr(self, action)(sid, **params)
                self.store.update(sid, job={"action": action, "state": "done", "t": time.time(), "msg": None})
            except NotEnoughMemory as e:
                self._fail(sid, action, str(e))
            except Exception as e:  # report to the UI, keep the worker alive
                log.error("%s %s: %s", action, sid, traceback.format_exc())
                self._fail(sid, action, str(e))

    def _fail(self, sid, action, msg):
        try:
            self.store.update(sid, job={"action": action, "state": "error", "t": time.time(), "msg": msg[:500]})
        except Exception:
            pass  # sample deleted meanwhile

    # ---- steps ----
    def _asr(self, y):
        # resample here: with a 24 kHz input the pipeline returns word times stretched by 24/16
        y16 = librosa.resample(y, orig_sr=SR, target_sr=16000)
        with self.models.use("asr") as asr:
            kw = {"chunk_length_s": 30} if len(y16) > 30 * 16000 else {}  # only long uploads need chunking
            out = asr({"raw": y16, "sampling_rate": 16000}, return_timestamps="word",
                      generate_kwargs={"language": "ukrainian", "task": "transcribe"}, **kw)
        words = [{"text": c["text"], "start": c["timestamp"][0], "end": c["timestamp"][1]} for c in out["chunks"]]
        return out["text"].strip(), words

    def transcribe(self, sid):
        text, words = self._asr(self.store.load_audio(sid))
        self.store.update(sid, asr_text=text, words=words)
        self.analyse(sid, asr=False)

    def analyse(self, sid, asr=True):
        m = self.store.get(sid)
        y = self.store.load_audio(sid)
        note = None
        if asr and m["asr_text"] is None:
            try:
                m["asr_text"], m["words"] = self._asr(y)
            except NotEnoughMemory as e:
                note = f"asr skipped: {e}"
        r = analyse(y, m["text"], asr_text=m["asr_text"])
        level, issues = verdict(r, bool(m["text"]))
        self.store.update(sid, asr_text=m["asr_text"], words=m["words"], analysis=r, verdict=level,
                          issues=issues, analysis_note=note, analysed_text=m["text"])

    def repair(self, sid, denoise="auto"):
        m = self.store.get(sid)
        if m["analysis"] is None or m.get("analysed_text") != m["text"]:
            self.analyse(sid)
            m = self.store.get(sid)
        y = self.store.load_audio(sid)

        def den(x):
            with self.models.use("sidon") as model:
                return sidon.restore(model, x, SR)

        fy, ftext, notes = repair(y, m["text"], m["analysis"], m["words"], den, denoise)
        self.store.write_fixed(sid, fy)
        try:
            asr_text, _ = self._asr(fy)
        except NotEnoughMemory:
            asr_text = None
        r = analyse(fy, ftext, asr_text=asr_text)
        level, issues = verdict(r, True)
        self.store.update(sid, fixed={"t": time.time(), "text": ftext, "notes": notes, "analysis": r,
                                      "verdict": level, "issues": issues, "denoise": denoise})
