#!/usr/bin/env python3
"""Lagswitch launcher (Windows .exe entry point).

This is the only part of the app that gets bundled into the frozen .exe and
that ever needs a rebuild. Its job is small and stays that way: on every
launch it fetches the latest lagswitch.py from the GitHub repo, compares it
to the cached copy on disk, and runs whichever copy is current -- offering
an in-app "Update" button instead of requiring a redownload of the .exe.

Edit lagswitch.py and push to GitHub; users see "Update" next time they open
the app and one click reloads the new code in-process.
"""

import hashlib
import importlib.util
import os
import sys
import urllib.request

RAW_URL = "https://raw.githubusercontent.com/d7mm555/7amanys-Lagswitch/main/lagswitch.py"
FETCH_TIMEOUT = 4


def _app_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "Lagswitch")
    os.makedirs(path, exist_ok=True)
    return path


def _bundled_payload_path():
    # PyInstaller --add-data extracts to sys._MEIPASS at runtime.
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "lagswitch.py")


def _fetch_remote_source():
    """Best-effort download of the latest payload. None if offline/failed."""
    try:
        with urllib.request.urlopen(RAW_URL, timeout=FETCH_TIMEOUT) as resp:
            return resp.read().decode("utf-8")
    except Exception:  # noqa: BLE001 - any network/SSL/HTTP failure -> offline
        return None


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_payload(path):
    spec = importlib.util.spec_from_file_location("lagswitch_payload", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    cache_path = os.path.join(_app_dir(), "lagswitch.py")

    if not os.path.exists(cache_path):
        seed = _fetch_remote_source()
        if seed is None:
            with open(_bundled_payload_path(), "r", encoding="utf-8") as fh:
                seed = fh.read()
        with open(cache_path, "w", encoding="utf-8") as fh:
            fh.write(seed)

    while True:
        remote_source = _fetch_remote_source()
        update_available = False
        if remote_source is not None:
            with open(cache_path, "r", encoding="utf-8") as fh:
                cached_source = fh.read()
            update_available = _sha256(remote_source) != _sha256(cached_source)

        payload = _load_payload(cache_path)
        action = payload.run(update_available)

        if action == "update" and remote_source is not None:
            with open(cache_path, "w", encoding="utf-8") as fh:
                fh.write(remote_source)
            continue

        break


if __name__ == "__main__":
    main()
