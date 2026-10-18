"""Local shortcut: reuse the public service or run it in this console."""
from __future__ import annotations
import argparse
import json
import socket
import threading
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def site_is_running(base: str) -> bool:
    # Ignore proxy variables for local readiness checks.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(base + "/health", timeout=1) as response:
            health = json.load(response)
        with opener.open(base + "/api/site", timeout=1) as response:
            site = json.load(response)
        return health.get("service") == "corpus-readonly-search" and site.get("read_only") is True
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description="Open or start the local public corpus website")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--port", type=int, default=8002)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    base = f"http://127.0.0.1:{args.port}"
    if site_is_running(base):
        print(f"Public site is already running: {base}/")
        if not args.no_browser:
            webbrowser.open(base + "/")
        return 0
    # Do not stop, replace or silently reuse another service on this port.
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", args.port))
        except OSError:
            print(f"Port {args.port} is occupied by another or not-yet-ready service. No process was stopped. Try again after startup completes.")
            return 1
    config = args.config or (Path("data/public_site/settings.json") if (ROOT / "data/public_site/settings.json").is_file() else Path("configs/public_site.example.json"))
    config = config if config.is_absolute() else ROOT / config
    if not config.is_file():
        print("Site configuration was not found. See docs for public-site setup.")
        return 1
    if args.config is None and config.name == "public_site.example.json":
        print("No private site settings found; opening an empty example. Select a Release and source archive to publish your corpus.")
    print(f"Starting public site: {base}/")
    print("Keep this console open while using the service. Press Ctrl+C here to stop this service.")
    done = threading.Event()
    def open_when_ready():
        for _ in range(120):
            if done.is_set():
                return
            if site_is_running(base):
                if not args.no_browser:
                    webbrowser.open(base + "/")
                return
            done.wait(0.5)
        print(f"Browser opening timed out. Check this console; when ready, open {base}/ manually.")
    watcher = threading.Thread(target=open_when_ready, daemon=True)
    watcher.start()
    try:
        from .public_site import main as serve
        return serve(["--config", str(config), "--project-root", str(ROOT), "--host", "127.0.0.1", "--port", str(args.port)])
    finally:
        done.set()


if __name__ == "__main__":
    raise SystemExit(main())
