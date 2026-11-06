"""Compatibility CLI launcher; implementation lives in intent_engine.entrypoints."""
import sys
from intent_engine.entrypoints import cli as _implementation

if __name__ == "__main__":
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
