"""Lazy model manager: loads models on first use, unloads them after idle time, and refuses to load
when there is not enough free memory (after unloading other idle models first)."""
import gc
import os
import threading
import time
from contextlib import contextmanager

import psutil
import torch

from . import sidon
from .config import ASR_MODELS, SIDON_MB, SIDON_REPO


class NotEnoughMemory(Exception):
    def __init__(self, model, need_mb, free_mb):
        super().__init__(f"memory: {model}: need ~{need_mb} MB, free {free_mb} MB")
        self.model, self.need_mb, self.free_mb = model, need_mb, free_mb


class ModelManager:
    def __init__(self, settings, cache_dir):
        self.settings = settings
        self.cache_dir = cache_dir
        self.lock = threading.RLock()
        self.models = {}  # name -> {"obj", "last", "busy", "device", "mb"}
        threading.Thread(target=self._reaper, daemon=True).start()

    # ---- device and memory ----
    def device(self):
        d = self.settings.get("device")
        if d == "auto":
            return "cuda" if torch.cuda.is_available() else "cpu"
        if d == "cuda" and not torch.cuda.is_available():
            return "cpu"
        return d

    def free_mb(self, device):
        ram = psutil.virtual_memory().available // 2**20
        if device == "cuda":
            free, _ = torch.cuda.mem_get_info()
            return min(free // 2**20, ram) if _unified() else free // 2**20
        return ram

    def _ensure_room(self, name, need_mb, device):
        reserve = self.settings.get("reserve_mb")
        if self.free_mb(device) >= need_mb + reserve:
            return
        # unload other idle models, least recently used first
        for other in sorted((n for n, m in self.models.items() if n != name and not m["busy"]),
                            key=lambda n: self.models[n]["last"]):
            self.unload(other)
            if self.free_mb(device) >= need_mb + reserve:
                return
        raise NotEnoughMemory(name, need_mb, self.free_mb(device))

    # ---- loading ----
    def _load(self, name, device):
        if name == "asr":
            from transformers import pipeline
            model_id = self.settings.get("asr_model")
            need = ASR_MODELS.get(model_id, (4000, 7000))[device == "cpu"]
            self._ensure_room(name, need, device)
            obj = pipeline("automatic-speech-recognition", model=model_id,
                           dtype=torch.float16 if device == "cuda" else torch.float32,
                           device=device, model_kwargs={"cache_dir": self.cache_dir})
            return obj, need
        if name == "sidon":
            need = SIDON_MB[device == "cpu"]
            self._ensure_room(name, need, device)
            return sidon.load(SIDON_REPO, device, self.cache_dir), need
        raise KeyError(name)

    @contextmanager
    def use(self, name):
        """with models.use("asr") as asr: ...  (keeps the model from being unloaded meanwhile)"""
        device = self.device()
        with self.lock:
            m = self.models.get(name)
            if m and m["device"] != device:
                self.unload(name)
                m = None
            if m is None:
                obj, mb = self._load(name, device)
                m = self.models[name] = {"obj": obj, "last": time.time(), "busy": 0, "device": device, "mb": mb}
            m["busy"] += 1
        try:
            yield m["obj"]
        finally:
            with self.lock:
                m["busy"] -= 1
                m["last"] = time.time()

    def unload(self, name):
        with self.lock:
            m = self.models.pop(name, None)
            if m is None:
                return
            del m["obj"]
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

    def unload_all(self):
        for n in list(self.models):
            self.unload(n)

    def _reaper(self):
        while True:
            time.sleep(10)
            idle = self.settings.get("idle_unload_s")
            with self.lock:
                for n, m in list(self.models.items()):
                    if not m["busy"] and time.time() - m["last"] > idle:
                        self.unload(n)

    def status(self):
        device = self.device()
        with self.lock:
            loaded = [{"name": n, "device": m["device"], "mb": m["mb"], "idle_s": int(time.time() - m["last"]),
                       "busy": m["busy"] > 0} for n, m in self.models.items()]
        return {"device": device, "cuda": torch.cuda.is_available(), "free_mb": self.free_mb(device),
                "loaded": loaded}


def _unified():
    """GPU memory shared with the host (Jetson / GB10-class systems): count host RAM too."""
    return os.path.exists("/etc/nv_tegra_release") or "GB10" in torch.cuda.get_device_name(0)
