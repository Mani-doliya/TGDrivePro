#!/usr/bin/env python3
"""Dependency-free static checks for a fresh checkout."""
import ast
from pathlib import Path

source = Path(__file__).with_name("TGDrive.py")
code = source.read_text(encoding="utf-8")
compile(code, str(source), "exec")
ast.parse(code)
required = [
    "def ensure_config():",
    "def show_password_screen(self):",
    "def upload_files(self):",
    "def download_selected(self):",
    "def show_settings(self):",
]
missing = [item for item in required if item not in code]
if missing:
    raise SystemExit("Missing expected application sections: " + ", ".join(missing))
print("TGDrive Pro static checks passed.")
