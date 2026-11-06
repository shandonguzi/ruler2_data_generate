"""Compatibility launcher / ASGI import for the packaged web application."""
import sys
from intent_engine.infrastructure.startup_timing import timed
if __name__ == "__main__":
    with timed("Web 应用模块加载（含依赖与历史恢复）"):
        from intent_engine.web import app as _implementation
else:
    from intent_engine.web import app as _implementation

if __name__ == "__main__":
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
