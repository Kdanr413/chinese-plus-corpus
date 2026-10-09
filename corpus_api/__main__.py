"""Run the local API with environment-configurable host and port."""

from __future__ import annotations

import os
import threading
import time
import urllib.request
import webbrowser

import uvicorn


def _port() -> int:
    raw = os.getenv("API_PORT", "8000")
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit("API_PORT must be an integer") from exc
    if not 1 <= value <= 65535:
        raise SystemExit("API_PORT must be between 1 and 65535")
    return value


def main() -> None:
    host = os.getenv("API_HOST", "127.0.0.1")
    port = _port()
    if os.getenv("API_OPEN_BROWSER", "0") == "1":
        def open_when_ready() -> None:
            url = f"http://127.0.0.1:{port}/"
            for _ in range(80):
                try:
                    with urllib.request.urlopen(f"{url}health", timeout=0.5):
                        webbrowser.open(url)
                        return
                except OSError:
                    time.sleep(0.25)

        threading.Thread(target=open_when_ready, daemon=True).start()
    uvicorn.run(
        "corpus_api.app:app",
        host=host,
        port=port,
        reload=False,
    )


if __name__ == "__main__":
    main()
