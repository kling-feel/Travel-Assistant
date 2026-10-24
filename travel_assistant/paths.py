"""Filesystem locations shared by the application."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = PROJECT_ROOT / "data" / "fixtures"
STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"
