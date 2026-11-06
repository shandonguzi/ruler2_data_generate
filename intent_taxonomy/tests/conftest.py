"""Lightweight dependency stubs for source-only CI environments.

The packaged application installs real ``openai`` and ``json-repair`` from
requirements.txt. These stubs are used only when the test runner environment
has not installed those optional runtime dependencies.
"""
from __future__ import annotations

import json
import sys
import types

try:
    import json_repair  # noqa: F401
except Exception:
    mod = types.ModuleType("json_repair")
    def repair_json(value, return_objects=False):
        if return_objects:
            return json.loads(value) if isinstance(value, str) else value
        return value
    mod.repair_json = repair_json
    sys.modules["json_repair"] = mod

try:
    import openai  # noqa: F401
except Exception:
    mod = types.ModuleType("openai")
    class OpenAI:
        def __init__(self, *args, **kwargs):
            self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=lambda **_: None))
            self.models = types.SimpleNamespace(list=lambda: types.SimpleNamespace(data=[]))
    mod.OpenAI = OpenAI
    sys.modules["openai"] = mod
