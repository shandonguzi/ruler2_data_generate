"""Monotonic, console-only startup timings (never print configuration secrets)."""
import os
import time
from contextlib import contextmanager

STARTED = time.perf_counter()


def report(label, started):
    if os.environ.get('INTENT_STARTUP_TIMING', '1') != '0':
        print(f'[启动耗时] {label}: {time.perf_counter()-started:.3f} 秒',flush=True)


@contextmanager
def timed(label):
    started=time.perf_counter()
    try:
        yield
    finally:
        report(label,started)
