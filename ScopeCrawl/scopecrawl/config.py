"""配置加载、.env 解析与校验。"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import SecretStr, ValidationError

from .models import AuthorityLists, Settings

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "config.yaml"
DEFAULT_ENV_PATH = CONFIG_DIR / ".env"
DEFAULT_AUTHORITY_PATH = CONFIG_DIR / "authority.yaml"

# 固定键名：只从 .env 文件读取，不读取进程环境变量。
TAVILY_API_KEY_ENV = "SCOPECRAWL_TAVILY_API_KEY"
LLM_API_KEY_ENV = "SCOPECRAWL_LLM_API_KEY"
LLM_BASE_URL_ENV = "SCOPECRAWL_LLM_BASE_URL"
LLM_MODEL_ENV = "SCOPECRAWL_LLM_MODEL"

_RUN_OVERRIDE_KEYS = ("s3", "s4", "location", "language", "time_range")


class ConfigError(Exception):
    """配置缺失或不合法，属于致命错误。"""


def load_settings(
    config_path: str | Path | None = None,
    overrides: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    env_path: str | Path | None = None,
    require_secrets: bool = True,
    project_root: Path | None = None,
) -> Settings:
    """加载配置：pydantic 默认值 → YAML → .env → 命令行覆盖。

    ``env`` 仅供测试注入，表示 .env 的内容；为 ``None`` 时读取 .env 文件，
    绝不读取进程环境变量。``overrides`` 中值为 ``None`` 表示未提供。
    """
    root = project_root or PROJECT_ROOT
    env_map = (
        _read_dotenv(
            _resolve_default(env_path, DEFAULT_ENV_PATH, root),
            required=require_secrets,
        )
        if env is None
        else dict(env)
    )

    path = _resolve_default(config_path, DEFAULT_CONFIG_PATH, root)
    data = _read_yaml(path)
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件格式错误，顶层必须是映射：{path}")

    _apply_run_overrides(data, overrides or {})

    try:
        settings = Settings.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from exc

    _validate_settings(settings)
    _resolve_secrets(settings, env_map, require_secrets=require_secrets)
    return settings


def load_authority(
    config_path: str | Path | None = None,
    *,
    project_root: Path | None = None,
) -> AuthorityLists:
    """读取白名单、黑名单与无法提取名单；文件缺失时返回空列表并告警。"""
    root = project_root or PROJECT_ROOT
    path = _resolve_default(config_path, DEFAULT_AUTHORITY_PATH, root)
    if not path.exists():
        logger.warning("权威度配置文件不存在，使用空白名单：%s", path)
        return AuthorityLists()

    data = _read_yaml(path) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"权威度配置格式错误：{path}")

    try:
        lists = AuthorityLists.model_validate(
            {
                "whitelist": _normalize_entries(data.get("whitelist")),
                "blacklist": _normalize_entries(data.get("blacklist")),
                "unfetchable": _normalize_entries(data.get("unfetchable")),
            }
        )
    except ValidationError as exc:
        raise ConfigError(f"权威度配置不合法：{_format_validation_error(exc)}") from exc

    for white in lists.whitelist:
        for black in lists.blacklist:
            if _patterns_overlap(white.domain, black.domain):
                raise ConfigError(f"白名单与黑名单冲突：{white.domain} / {black.domain}")
    return lists


def _normalize_entries(entries: Any) -> list[dict[str, str]]:
    if entries is None:
        return []
    if not isinstance(entries, Sequence) or isinstance(entries, str):
        raise ConfigError("名单必须是列表")
    normalized: list[dict[str, str]] = []
    for entry in entries:
        if isinstance(entry, str):
            normalized.append({"domain": entry, "reason": ""})
        elif isinstance(entry, dict) and entry.get("domain"):
            normalized.append(
                {
                    "domain": str(entry["domain"]),
                    "reason": str(entry.get("reason", "")),
                }
            )
        else:
            raise ConfigError(f"无效的名单条目：{entry!r}")
    return normalized


def _patterns_overlap(left: str, right: str) -> bool:
    a = left.lower().strip()
    b = right.lower().strip()
    if a == b:
        return True
    a_base = a[2:] if a.startswith("*.") else a
    b_base = b[2:] if b.startswith("*.") else b
    if a.startswith("*.") and (b_base == a_base or b_base.endswith("." + a_base)):
        return True
    if b.startswith("*.") and (a_base == b_base or a_base.endswith("." + b_base)):
        return True
    return False


def resolve_path(path: str | Path, *, root: Path = PROJECT_ROOT) -> Path:
    """绝对路径原样返回，相对路径以 root 为基准。"""
    value = Path(path)
    return value if value.is_absolute() else root / value


def _resolve_default(candidate: str | Path | None, default: Path, root: Path) -> Path:
    """未显式提供路径时，使用相对于项目根目录的默认路径（如 config/.env）。"""
    path = Path(candidate) if candidate is not None else default.relative_to(PROJECT_ROOT)
    return resolve_path(path, root=root)


def _read_yaml(path: Path) -> Any:
    if not path.exists():
        raise ConfigError(f"配置文件不存在：{path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 解析失败：{path}：{exc}") from exc
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件：{path}：{exc}") from exc


def _apply_run_overrides(data: dict[str, Any], overrides: Mapping[str, Any]) -> None:
    run = data.setdefault("run", {})
    if not isinstance(run, dict):
        raise ConfigError("配置中的 run 段必须是映射")
    for key in _RUN_OVERRIDE_KEYS:
        value = overrides.get(key)
        if value is None:
            continue
        if key == "location" and value == "":
            value = None
        run[key] = value


def _validate_settings(settings: Settings) -> None:
    """只保留 pydantic Field 无法表达的去空白与跨字段校验。"""
    if not settings.run.s3.strip():
        raise ConfigError("run.s3 不能为空")
    if not settings.run.s4.strip():
        raise ConfigError("run.s4 不能为空")

    thresholds = settings.review.thresholds
    if not (0 <= thresholds.low < thresholds.medium < thresholds.high <= 1):
        raise ConfigError("review.thresholds 必须满足 0 <= low < medium < high <= 1")

    scoring_weights = settings.authority.scoring.weights
    if abs(sum(scoring_weights.values()) - 1.0) > 1e-6:
        raise ConfigError("authority.scoring.weights 之和必须为 1")

    if (
        not 0
        <= settings.authority.scoring.low_threshold
        < settings.authority.scoring.high_threshold
        <= 1
    ):
        raise ConfigError("authority.scoring 阈值必须满足 0 <= low < high <= 1")


def _resolve_secrets(
    settings: Settings,
    env: Mapping[str, str],
    *,
    require_secrets: bool,
) -> None:
    model = _env_value(env, LLM_MODEL_ENV)
    tavily_key = _env_value(env, TAVILY_API_KEY_ENV)
    llm_key = _env_value(env, LLM_API_KEY_ENV)
    llm_base_url = _env_value(env, LLM_BASE_URL_ENV)

    if require_secrets:
        if not model:
            raise ConfigError(f"缺少 LLM 模型名：请在 .env 设置 {LLM_MODEL_ENV}")
        if not tavily_key:
            raise ConfigError(f"缺少搜索 API Key：请在 .env 设置 {TAVILY_API_KEY_ENV}")
        if not llm_key:
            raise ConfigError(f"缺少 LLM API Key：请在 .env 设置 {LLM_API_KEY_ENV}")

    settings.llm_model = model or None
    settings.tavily_api_key = SecretStr(tavily_key) if tavily_key else None
    settings.llm_api_key = SecretStr(llm_key) if llm_key else None
    settings.llm_base_url = llm_base_url or None


def _read_dotenv(path: Path, *, required: bool) -> dict[str, str]:
    """读取 .env 文件；支持注释、export 前缀和单双引号。"""
    if not path.exists():
        if required:
            raise ConfigError(f".env 文件不存在：{path}")
        return {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"无法读取 .env 文件：{path}：{exc}") from exc

    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def _env_value(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    return value.strip() if value else ""


def _format_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"])
        parts.append(f"{location}: {error['msg']}")
    return "配置校验失败：" + "; ".join(parts)
