"""Vercel entry point.

Vercel's Python runtime looks for an ASGI ``app`` in this module. The project
root is added to ``sys.path`` so the function can import the same modules used
by the local servers.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web_app import app  # noqa: E402,F401
