#!/usr/bin/env python3
"""Execute the actual Luau entries against a tiny in-memory Noctalia host."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
sources = "\n".join(
    f"local {name}Source = [====[{(root / 'multica' / (name + '.luau')).read_text()}]====]"
    for name in ["panel", "widget", "client", "service"]
)
with tempfile.TemporaryDirectory() as tmp:
    script = Path(tmp) / "tests.luau"
    script.write_text(sources + "\n" + (root / "tests/ui.luau").read_text())
    subprocess.run(["luau", str(script)], check=True)
