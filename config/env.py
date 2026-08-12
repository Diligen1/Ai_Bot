"""Minimal .env reader — stdlib only, no external dependency (no python-dotenv).

Reads `.env` from the project root (KEY=VALUE per line; blank lines and '#'
comments ignored) once, and caches the parsed values for the process
lifetime. Real process environment variables (os.environ) always take
priority over the file, so a real deployment can still inject secrets the
normal way.

Values read here (e.g. GEMINI_API_KEY) are only ever handed to the specific
component that needs them (see ai/gemini_provider.py) — this module never
writes them to a database, a log, or any other file.
"""
from __future__ import annotations

import os
from pathlib import Path

_env_file_cache: dict[str, str] | None = None


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_env_file() -> dict[str, str]:
    global _env_file_cache
    if _env_file_cache is not None:
        return _env_file_cache
    values: dict[str, str] = {}
    env_path = _project_root() / '.env'
    if env_path.exists():
        for line in env_path.read_text(encoding='utf-8').splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith('#') or '=' not in stripped:
                continue
            key, _, value = stripped.partition('=')
            values[key.strip()] = value.strip().strip('"').strip("'")
    _env_file_cache = values
    return values


def get_env(key: str, default: str | None = None) -> str | None:
    """os.environ wins if the key is set there; otherwise falls back to .env."""
    if key in os.environ:
        return os.environ[key]
    return _load_env_file().get(key, default)
