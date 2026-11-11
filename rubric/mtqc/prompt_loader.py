"""Load prompt contents and hash the actual bytes, not just version labels."""
from hashlib import sha256
from pathlib import Path


def load_prompt(path: Path) -> dict:
    data = path.read_bytes()
    content = data.decode("utf-8")
    if not content.strip():
        raise ValueError(f"Empty prompt: {path}")
    return {"content": content, "sha256": sha256(data).hexdigest()}
