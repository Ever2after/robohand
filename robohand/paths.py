"""Filesystem layout shared by every Robohand tool.

Code lives in the repository; everything a session produces (recordings, snapshots,
calibrations, settings, logs) lives under DATA_ROOT, which defaults to ``<repo>/data``
and can be moved with the ROBOHAND_DATA environment variable, e.g. to keep using an
existing data folder.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent
ASSETS = PACKAGE_ROOT / "assets"
APP_ICON = ASSETS / "app.ico"

DATA_ROOT = Path(os.environ.get("ROBOHAND_DATA") or REPO_ROOT / "data").resolve()
RECORDINGS = DATA_ROOT / "recordings"
CAPTURES = DATA_ROOT / "captures"
CALIBRATIONS = DATA_ROOT / "calibrations"
SETTINGS = DATA_ROOT / "settings.json"
ACTIVE_CALIBRATION = DATA_ROOT / "calibration.json"
LOGS = DATA_ROOT / "logs"

TRACKER_STUDIO = REPO_ROOT / "tracker_studio"
MANUS_SDK_DIR = Path(os.environ.get("MANUS_SDK_DIR") or r"C:\Program Files\MANUS\Core 3\ManusCore")


def configure_logging(name):
    """Log to DATA_ROOT/logs/<name>.log (GUI tools often run without a console)."""
    LOGS.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=LOGS / f"{name}.log", level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return LOGS / f"{name}.log"
