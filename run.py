"""Entry point: python run.py [--host 127.0.0.1] [--port 8780] [--data ./data] [--token SECRET]"""
import argparse
import logging
import os

from doctor.web import create_app


def main():
    ap = argparse.ArgumentParser(description="OmniVoice uk samples doctor")
    ap.add_argument("--host", default=os.environ.get("DOCTOR_HOST", "127.0.0.1"),
                    help="0.0.0.0 exposes the service to the network")
    ap.add_argument("--port", type=int, default=int(os.environ.get("DOCTOR_PORT", "8780")))
    ap.add_argument("--data", default=os.environ.get("DOCTOR_DATA", "data"), help="samples, settings, model cache")
    ap.add_argument("--token", default=os.environ.get("DOCTOR_TOKEN"), help="require this token for the API")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if a.host not in ("127.0.0.1", "localhost", "::1") and not a.token:
        logging.warning("listening on %s without --token: anyone on the network can use the service", a.host)
    app = create_app(a.data, a.token)
    try:
        from waitress import serve
        serve(app, host=a.host, port=a.port, threads=8)
    except ImportError:
        app.run(host=a.host, port=a.port, threaded=True)


if __name__ == "__main__":
    main()
