"""Central configuration and paths.

Secrets are read from a local ``.env`` file (see ``.env.example``) and are
never hardcoded or committed. Missing tokens are allowed — the sources that
need them simply skip themselves and report a warning during refresh.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "startups.db"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

load_dotenv(BASE_DIR / ".env")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
PRODUCT_HUNT_TOKEN = os.getenv("PRODUCT_HUNT_TOKEN", "").strip()
SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT",
    "Startup Tracker (personal research tool) anonymous@example.com",
).strip()

# Shared HTTP timeout (seconds) for every outbound source request.
HTTP_TIMEOUT = 20.0

DATA_DIR.mkdir(exist_ok=True)
