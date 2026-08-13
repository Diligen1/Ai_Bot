"""Entrypoint runner for the AI Futures Bot dashboard."""

import socket
import subprocess
import sys

import uvicorn

from config.env import get_env

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

# Some Windows consoles run a non-UTF-8 codepage (e.g. cp1252), which makes
# print() raise UnicodeEncodeError on the Cyrillic messages below. Force
# UTF-8 with a safe fallback so the diagnostic message can never itself
# crash the script.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _get_configured_port() -> int:
    raw = get_env("APP_PORT")
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_PORT


def _is_port_free(host: str, port: int) -> bool:
    """Mirrors what uvicorn itself is about to do: bind to (host, port)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
        return True


def _find_pid_on_port(port: int) -> str | None:
    """Best-effort PID lookup via `netstat -ano` (Windows). Returns None if
    netstat isn't available or no listening entry for the port is found —
    never raises, this is purely informational."""
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    needle = f":{port}"
    for line in result.stdout.splitlines():
        if needle in line and "LISTENING" in line:
            parts = line.split()
            if parts:
                return parts[-1]
    return None


def main_runner() -> None:
    host = DEFAULT_HOST
    port = _get_configured_port()

    if not _is_port_free(host, port):
        print(f"Порт {port} уже занят. Возможно, бот уже запущен.")
        pid = _find_pid_on_port(port)
        if pid:
            print(f"PID процесса на порту {port}: {pid}")
        print(f"Проверить вручную (Windows): netstat -ano | findstr :{port}")
        return

    print("Starting AI Futures Bot dashboard")
    print(f"http://{host}:{port}")
    uvicorn.run("app.server:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main_runner()
