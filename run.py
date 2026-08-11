"""Entrypoint runner for the AI Futures Bot dashboard."""

import uvicorn


def main_runner() -> None:
    print("Starting AI Futures Bot dashboard")
    print("http://127.0.0.1:8000")
    uvicorn.run("app.server:app", host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main_runner()
