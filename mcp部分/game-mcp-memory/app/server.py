"""Run Uvicorn with the same settings used by the application."""

from __future__ import annotations

import uvicorn

from app.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.memory_host,
        port=settings.memory_port,
    )


if __name__ == "__main__":
    main()
