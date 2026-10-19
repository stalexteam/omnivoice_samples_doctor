"""Flask app: JSON API + static single-page UI (Ukrainian)."""
import hmac
import io
import json
import os
import re
import tempfile
import zipfile
from urllib.parse import quote

import numpy as np
from flask import Flask, abort, jsonify, request, send_file, send_from_directory

from .analysis import speech_intervals
from .config import ASR_MODELS, INFO_DEFAULTS, SR, Settings
from .jobs import ACTIONS, Worker
from .models import ModelManager
from .store import AUDIO_EXT, Store, StoreError, clean_name, clean_text

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_UPLOAD_MB = 64
MAX_INFO_BYTES = 64 * 1024
MAX_SAMPLES = 1000
INFO_FIELDS = {"name": lambda v: clean_name(v), "cfg_strength": float, "speed": float}  # editable .info keys

try:  # optional: stressed transcript for the exported .info
    from accentuk import Accentor
    _acc = Accentor(mode="light")
except Exception:
    _acc = None


def create_app(data_dir, token=None):
    data_dir = os.path.realpath(data_dir)
    os.makedirs(data_dir, exist_ok=True)
    tmp_dir = os.path.join(data_dir, "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    settings = Settings(os.path.join(data_dir, "settings.json"))
    store = Store(data_dir)
    models = ModelManager(settings, os.path.join(data_dir, "models"))
    worker = Worker(store, models)
    _peaks = {}  # (path, mtime) -> waveform envelope

    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 2**20
    app.json.ensure_ascii = False
    app.json.sort_keys = False

    @app.before_request
    def auth():
        if not token or request.path == "/" or request.path.startswith("/static/"):
            return
        given = request.headers.get("X-Token") or request.args.get("t") or ""
        if not hmac.compare_digest(given.encode(), token.encode()):
            abort(401)

    @app.after_request
    def headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; media-src 'self' blob:; img-src 'self' data:; "
                                                   "style-src 'self'; script-src 'self'; frame-ancestors 'none'")
        return resp

    @app.errorhandler(StoreError)
    def store_error(e):
        return jsonify(error=str(e)), 404 if "not found" in str(e) else 400

    # ---- UI ----
    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/static/<path:name>")
    def static_file(name):
        return send_from_directory(STATIC, name)  # safe_join: no escape from STATIC

    # ---- samples ----
    def summary(m):
        return {k: m.get(k) for k in ("id", "name", "created", "verdict", "job")} | {
            "fixed_verdict": (m.get("fixed") or {}).get("verdict")}

    @app.get("/api/samples")
    def list_samples():
        return jsonify([summary(m) for m in store.list()])

    @app.post("/api/samples")
    def upload():
        files = request.files.getlist("files")
        if not files:
            return jsonify(error="no files"), 400
        if len(store.list()) + len(files) > MAX_SAMPLES:
            return jsonify(error="too many samples"), 400
        infos = {}
        for f in files:  # .info / .json next to audio: OmniVoice voice description with "ref_text"
            stem, ext = os.path.splitext(clean_name(f.filename))
            if ext.lower() in (".info", ".json"):
                raw = f.read(MAX_INFO_BYTES + 1)
                d = {}
                if len(raw) <= MAX_INFO_BYTES:
                    for enc in ("utf-8-sig", "cp1251"):  # Notepad BOM, legacy Windows files
                        try:
                            d = json.loads(raw.decode(enc))
                            break
                        except ValueError:  # UnicodeDecodeError and JSONDecodeError are both ValueError
                            continue
                infos[stem] = d if isinstance(d, dict) else {}
        form_text = clean_text(request.form.get("text"))
        audio = [f for f in files if os.path.splitext(clean_name(f.filename))[1].lower() in AUDIO_EXT]
        created, errors = [], []
        for f in files:
            ext = os.path.splitext(clean_name(f.filename))[1].lower()
            if ext not in AUDIO_EXT and ext not in (".info", ".json"):
                errors.append({"name": clean_name(f.filename), "error": "unsupported file type"})
        for f in audio:
            name = clean_name(f.filename)
            stem = os.path.splitext(name)[0]
            info = infos.get(stem, {})
            text = clean_text(info.get("ref_text")) or (form_text if len(audio) == 1 else None)
            fd, tmp = tempfile.mkstemp(dir=tmp_dir, suffix=os.path.splitext(name)[1].lower())
            os.close(fd)
            try:
                f.save(tmp)
                m = store.create(tmp, name, text)
                extra = {"name": stem, **INFO_DEFAULTS}
                extra.update({k: v for k, v in info.items() if k not in ("ref_text", "stressed_ref_text")
                              and isinstance(v, (str, int, float, bool)) and len(str(k)) < 64})
                m = store.update(m["id"], info_extra=extra)
                created.append(summary(m))
                if request.form.get("analyse", "1") == "1":
                    worker.submit(m["id"], "analyse")
            except StoreError as e:
                errors.append({"name": name, "error": str(e)})
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        return jsonify(created=created, errors=errors)

    @app.get("/api/samples/<sid>")
    def get_sample(sid):
        m = store.get(sid)
        m.pop("words", None)
        return jsonify(m)

    @app.patch("/api/samples/<sid>")
    def patch_sample(sid):
        d = request.get_json(silent=True) or {}
        kw = {}
        if "text" in d:
            kw["text"] = clean_text(d["text"])
        if "name" in d:
            kw["name"] = clean_name(d["name"])
        if "info" in d and isinstance(d["info"], dict):  # .info fields other than the transcript
            extra = dict(store.get(sid).get("info_extra") or {})
            for k, v in d["info"].items():
                if k in INFO_FIELDS:
                    try:
                        extra[k] = INFO_FIELDS[k](v) if v not in (None, "") else None
                    except (TypeError, ValueError):
                        pass
            kw["info_extra"] = {k: v for k, v in extra.items() if v is not None}
        m = store.update(sid, **kw)
        if "text" in kw and m["analysis"] is not None:
            worker.submit(sid, "analyse")
        return jsonify(summary(m))

    @app.delete("/api/samples/<sid>")
    def delete_sample(sid):
        store.delete(sid)
        return jsonify(ok=True)

    @app.post("/api/samples/<sid>/<action>")
    def run_action(sid, action):
        if action not in ACTIONS:
            abort(404)
        store.get(sid)  # validates the id
        params = {}
        if action == "repair":
            d = request.get_json(silent=True) or {}
            params["denoise"] = d.get("denoise") if d.get("denoise") in ("auto", "on", "off") else "auto"
        worker.submit(sid, action, **params)
        return jsonify(ok=True)

    @app.get("/api/samples/<sid>/audio/<kind>")
    def audio(sid, kind):
        if kind not in ("source", "fixed"):
            abort(404)
        return send_file(store.path(sid, kind), mimetype="audio/wav", conditional=True)

    @app.get("/api/samples/<sid>/peaks/<kind>")
    def peaks(sid, kind):
        """Waveform envelope (min/max per column) and speech intervals for the UI strip."""
        if kind not in ("source", "fixed"):
            abort(404)
        p = store.path(sid, kind)
        key = (p, os.path.getmtime(p))
        if key not in _peaks:
            y = store.load_audio(sid, kind)
            n = 900
            cols = np.array_split(y, n) if len(y) >= n else [y]
            env = [[round(float(c.min()), 3), round(float(c.max()), 3)] for c in cols if len(c)]
            iv = speech_intervals(y)
            if len(_peaks) > 64:
                _peaks.clear()
            _peaks[key] = {"dur": round(len(y) / SR, 3), "peaks": env,
                           "speech": [[round(a / SR, 3), round(b / SR, 3)] for a, b in iv]}
        return jsonify(_peaks[key])

    @app.get("/api/samples/<sid>/download/<kind>")
    def download(sid, kind):
        """zip: audio + <name>.info (OmniVoice voice description); fixed -> mp3, source -> the uploaded file."""
        if kind not in ("source", "fixed"):
            abort(404)
        m = store.get(sid)
        p = store.path(sid, "fixed_mp3" if kind == "fixed" else "original")
        text = (m.get("fixed") or {}).get("text") if kind == "fixed" else (m["text"] or m["asr_text"])
        info = {"name": os.path.splitext(m["name"])[0], **INFO_DEFAULTS, **(m.get("info_extra") or {})}
        info["ref_text"] = text or ""
        if _acc is not None and text:
            try:
                info["stressed_ref_text"] = _acc.stress(text).text
            except Exception:
                pass
        stem = re.sub(r"[^\w.-]+", "_", os.path.splitext(m["name"])[0], flags=re.UNICODE).strip("._") or "sample"
        stem += ".fixed" if kind == "fixed" else ""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(p, stem + (".mp3" if kind == "fixed" else m["orig_ext"]))
            z.writestr(stem + ".info", json.dumps(info, ensure_ascii=False, indent="\t"))
        buf.seek(0)
        resp = send_file(buf, mimetype="application/zip", as_attachment=True, download_name="sample.zip")
        resp.headers["Content-Disposition"] = f"attachment; filename=sample.zip; filename*=UTF-8''{quote(stem)}.zip"
        return resp

    # ---- status / settings ----
    @app.get("/api/status")
    def status():
        return jsonify(models.status() | {"queue": worker.pending(), "settings": settings.data,
                                          "asr_models": list(ASR_MODELS), "accentuk": _acc is not None})

    @app.post("/api/settings")
    def set_settings():
        d = request.get_json(silent=True) or {}
        kw = {}
        if d.get("device") in ("auto", "cuda", "cpu"):
            kw["device"] = d["device"]
        if d.get("asr_model") in ASR_MODELS:
            kw["asr_model"] = d["asr_model"]
        for k, lo, hi in (("idle_unload_s", 30, 86400), ("reserve_mb", 0, 65536)):  # clamp to sane ranges
            if k in d:
                try:
                    kw[k] = min(max(int(d[k]), lo), hi)
                except (TypeError, ValueError):
                    pass
        old = dict(settings.data)
        new = settings.update(**kw)
        if new["device"] != old["device"] or new["asr_model"] != old["asr_model"]:
            models.unload_all()
        return jsonify(new)

    @app.post("/api/unload")
    def unload():
        models.unload_all()
        return jsonify(ok=True)

    return app
