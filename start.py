#!/usr/bin/env python3
"""One command to install and run pokedeals:

    python start.py

The first time it creates a private virtual environment (.venv) and installs the requirements; every time it
then runs `python -m pokedeals start` inside it. That asks for your eBay keys on the very first start,
downloads the card catalogue in the background, and opens the site. Needs only Python 3.11 or newer.
Extra arguments go to `pokedeals start` (for example `--port 9000` or `--no-browser`). `python start.py update`
updates the code; `backup` and `restore` save and reload your watchlist and settings.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
MIN_PYTHON = (3, 11)
UTILITIES = ("update", "backup", "restore", "stats", "prices", "sync")      # run instead of starting the site


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _stamp_file() -> Path:
    return VENV / ".requirements.sha256"


def _requirements_hash() -> str:
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def needs_install() -> bool:
    """True when the requirements changed since the last install (or there was none)."""
    try:
        return _stamp_file().read_text(encoding="utf-8").strip() != _requirements_hash()
    except OSError:
        return True


def ensure_environment() -> None:
    if not venv_python().exists():
        print("Creating a private Python environment (first start only)...")
        venv.create(VENV, with_pip=True)
    if needs_install():
        print("Installing requirements (first start, or after an update)...")
        subprocess.check_call([str(venv_python()), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                               "-r", str(REQUIREMENTS)])
        _stamp_file().write_text(_requirements_hash(), encoding="utf-8")


def main(argv: list[str]) -> int:
    if sys.version_info < MIN_PYTHON:
        print(f"pokedeals needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer; this is {sys.version.split()[0]}.")
        return 1
    ensure_environment()
    command = [argv[0]] if argv and argv[0] in UTILITIES else ["start", *argv]     # e.g. `python start.py update`
    try:
        return subprocess.call([str(venv_python()), "-m", "pokedeals", *command, *(argv[1:] if command[0] != "start" else [])],
                               cwd=ROOT)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
