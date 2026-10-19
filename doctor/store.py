"""Sample storage. Every sample lives in <data>/samples/<id>/ with fixed file names; ids are generated
here (32 hex chars) and validated on every access, so no client-supplied string ever becomes a path."""
import json
import os
import re
import shutil
import threading
import time
import uuid

import subprocess

import librosa
import numpy as np
import soundfile as sf

from .config import SR

ID_RE = re.compile(r"^[0-9a-f]{32}$")
AUDIO_EXT = {".mp3", ".wav", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".webm"}
# kind -> file name inside the sample dir: decoded wav for processing / playback, mp3 for download;
# the uploaded file is kept as original<ext> (ext from AUDIO_EXT, recorded in meta)
FILES = {"source": "source.wav", "fixed": "fixed.wav", "fixed_mp3": "fixed.mp3"}
MAX_TEXT = 2000
MAX_NAME = 120


class StoreError(Exception):
    pass


class Store:
    def __init__(self, root):
        self.root = os.path.realpath(os.path.join(root, "samples"))
        os.makedirs(self.root, exist_ok=True)
        self.lock = threading.RLock()

    def _dir(self, sid):
        if not isinstance(sid, str) or not ID_RE.match(sid):
            raise StoreError("bad id")
        d = os.path.realpath(os.path.join(self.root, sid))
        if os.path.dirname(d) != self.root or not os.path.isdir(d):
            raise StoreError("not found")
        return d

    def path(self, sid, kind):
        """Absolute path of a sample file; only known kinds, only inside the store."""
        if kind == "original":
            ext = self.get(sid).get("orig_ext")
            if ext not in AUDIO_EXT:
                raise StoreError("not found")
            name = "original" + ext
        elif kind in FILES:
            name = FILES[kind]
        else:
            raise StoreError("bad kind")
        p = os.path.realpath(os.path.join(self._dir(sid), name))
        if not p.startswith(self.root + os.sep) or not os.path.isfile(p):
            raise StoreError("not found")
        return p

    # ---- meta ----
    def get(self, sid):
        with self.lock:
            return json.load(open(os.path.join(self._dir(sid), "meta.json"), encoding="utf-8"))

    def save(self, meta):
        with self.lock:
            p = os.path.join(self._dir(meta["id"]), "meta.json")
            tmp = p + ".tmp"
            json.dump(meta, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            os.replace(tmp, p)

    def update(self, sid, **kw):
        with self.lock:
            m = self.get(sid)
            m.update(kw)
            self.save(m)
            return m

    def list(self):
        out = []
        for sid in os.listdir(self.root):
            if ID_RE.match(sid):
                try:
                    out.append(self.get(sid))
                except (StoreError, OSError, ValueError):
                    continue
        return sorted(out, key=lambda m: m["created"], reverse=True)

    # ---- create / delete ----
    def create(self, upload_path, orig_name, text=None):
        """Decode an uploaded file (already saved to a temp path) into a new sample."""
        ext = os.path.splitext(orig_name)[1].lower()
        if ext not in AUDIO_EXT:
            raise StoreError("unsupported file type")
        try:
            y, _ = librosa.load(upload_path, sr=SR, mono=True)
        except Exception:
            raise StoreError("cannot decode audio")
        if len(y) < SR // 2:
            raise StoreError("audio too short")
        y = np.clip(y, -1.0, 1.0)
        sid = uuid.uuid4().hex
        d = os.path.join(self.root, sid)
        os.makedirs(d)
        sf.write(os.path.join(d, FILES["source"]), y, SR)
        shutil.copyfile(upload_path, os.path.join(d, "original" + ext))  # uploaded bytes, untouched
        meta = {"id": sid, "name": clean_name(orig_name), "created": time.time(), "text": clean_text(text),
                "asr_text": None, "words": None, "analysis": None, "verdict": None, "issues": [],
                "fixed": None, "job": None, "orig_ext": ext}
        json.dump(meta, open(os.path.join(d, "meta.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        return meta

    def delete(self, sid):
        with self.lock:
            shutil.rmtree(self._dir(sid))

    def load_audio(self, sid, kind="source"):
        y, _ = sf.read(self.path(sid, kind), dtype="float32")
        return y

    def write_fixed(self, sid, y):
        d = self._dir(sid)
        sf.write(os.path.join(d, FILES["fixed"]), y, SR)
        write_mp3(os.path.join(d, FILES["fixed_mp3"]), y)


def write_mp3(path, y):
    """MP3 via libsndfile (>= 1.1); ffmpeg as a fallback."""
    try:
        sf.write(path, y, SR, format="MP3", bitrate_mode="CONSTANT", compression_level=0.1)
        return
    except (TypeError, RuntimeError, sf.LibsndfileError):
        pass
    ff = shutil.which("ffmpeg")
    if not ff:
        raise StoreError("cannot write mp3: libsndfile without mp3 and no ffmpeg")
    wav = path[:-4] + ".tmp.wav"
    sf.write(wav, y, SR)
    try:
        subprocess.run([ff, "-y", "-loglevel", "error", "-i", wav, "-b:a", "192k", path], check=True)
    finally:
        os.unlink(wav)


def clean_text(t):
    if t is None:
        return None
    t = re.sub(r"\s+", " ", str(t)).strip()[:MAX_TEXT]
    return t or None


def clean_name(n):
    n = os.path.basename(str(n).replace("\\", "/"))
    n = re.sub(r"[\x00-\x1f\x7f]", "", n).strip()[:MAX_NAME]
    return n or "sample"
