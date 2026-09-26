"""Paths and model settings; importing this module never contacts a service."""
from pathlib import Path
from hashlib import sha256
import os

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "outputs"
MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-5.6-luna")


def source_hashes():
    """Version entry points and packaged code using stable tutorial-relative paths.

    Only these code directories participate; generated outputs, test artifacts,
    environment files and notebook exports are not configuration inputs.
    """
    paths = [*ROOT.glob("*.py"), *(ROOT / "formaggio").rglob("*.py"),
             *(ROOT / "scripts").rglob("*.py")]
    return {p.relative_to(ROOT).as_posix(): sha256(p.read_bytes()).hexdigest()
            for p in sorted(paths)}


def load_json(name: str):
    import json
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))
