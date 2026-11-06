"""Atomic JSON replacement with bounded retries for transient file locks."""
import json
import os
import tempfile
import time
from pathlib import Path


def replace_with_retry(source, target):
    delays = (0.05, 0.1, 0.2, 0.4, 0.4)
    for attempt in range(len(delays) + 1):
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            locked = isinstance(exc, PermissionError) or getattr(exc, 'winerror', None) in {5, 32, 33}
            if not locked or attempt == len(delays):
                raise
            time.sleep(delays[attempt])


def write_json_atomically(path, value, *, indent=None, sync=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        # Close the temporary file before replacement, including on Windows.
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=indent, allow_nan=False)
            if sync:
                stream.flush()
                os.fsync(stream.fileno())
        replace_with_retry(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # Cleanup must not hide the original replacement/serialization error.
                pass
