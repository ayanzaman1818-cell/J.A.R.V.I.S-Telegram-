"""Standalone zero-dependency script to package jarvis_rebuild.zip from the workspace."""

from __future__ import annotations

import pathlib
import zipfile

PACKAGE_PATHS = [
    ".env.example",
    "requirements.txt",
    "config.py",
    "main.py",
    "database/__init__.py",
    "database/models.py",
    "database/repository.py",
    "memory/__init__.py",
    "tools/__init__.py",
    "tools/base.py",
    "tools/web.py",
    "tools/browser.py",
    "tools/news_weather.py",
    "tools/productivity.py",
    "tools/memory_tools.py",
    "tools/comms.py",
    "tools/integrations.py",
    "brain/__init__.py",
    "brain/history.py",
    "brain/gemini.py",
    "brain/agent.py",
    "services/__init__.py",
    "services/telegram.py",
    "services/voice.py",
    "services/briefing.py",
    "services/scheduler.py",
    "tests/test_database.py",
    "tests/test_tools.py",
    "tests/test_gemini_reliability.py",
]


def build_zip() -> None:
    base = pathlib.Path(__file__).resolve().parent
    zip_path = base / "jarvis_rebuild.zip"
    count = 0
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel_path in PACKAGE_PATHS:
            target = base / rel_path
            if target.exists():
                zf.write(target, arcname=rel_path)
                count += 1
    print(f"Created {zip_path} with {count} files.")


if __name__ == "__main__":
    build_zip()
