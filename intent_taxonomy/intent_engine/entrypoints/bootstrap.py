from __future__ import annotations

from pathlib import Path

from intent_engine.settings.config import load_config
from intent_engine.infrastructure.utils import ensure_dir


def main() -> None:
    from intent_engine.paths import PROJECT_ROOT
    root = PROJECT_ROOT
    from intent_engine.infrastructure.startup_timing import timed
    with timed("配置读取与校验"):
        cfg = load_config(str(root / "config" / "config.yaml"))
    with timed("数据目录初始化"):
        for key in ("reports_dir", "checkpoint_dir"):
            ensure_dir(root / cfg["paths"][key])
        ensure_dir(root / "data" / "uploads")
    print(f"[OK] reports={root / cfg['paths']['reports_dir']}")
    print(f"[OK] checkpoints={root / cfg['paths']['checkpoint_dir']}")


if __name__ == "__main__":
    main()
