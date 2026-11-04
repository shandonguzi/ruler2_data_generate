from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .question_types import QUESTION_TYPES, QuestionType


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChunkConfig(StrictModel):
    size: int = Field(650, ge=64)
    overlap: int = Field(70, ge=0)

    @model_validator(mode="after")
    def check_overlap(self):
        if self.overlap >= self.size:
            raise ValueError("chunking.overlap must be smaller than chunking.size")
        return self


class ContextConfig(StrictModel):
    strategy: Literal["chunks", "whole_document"] = "whole_document"
    embedding: Literal["tfidf", "openai"] = "tfidf"
    embedding_model: str = ""
    cross_document: bool = False
    max_chunks: int = Field(3, ge=1)
    max_tokens: int = Field(6000, ge=128)
    max_contexts_per_document: int = Field(8, ge=1)
    max_cross_contexts: int = Field(10, ge=0)
    similarity_threshold: float = Field(0.12, ge=0, le=1)
    entities: list[str] = Field(default_factory=list)
    relations: list[str] = Field(default_factory=list)


class SynthesisConfig(StrictModel):
    model: str = ""
    judge_model: str = ""
    document_rules: dict[str, list[str]] = Field(default_factory=dict)
    context_rules: dict[str, list[str]] = Field(default_factory=dict)
    max_goldens_per_context: int = Field(10, ge=1, le=20)
    max_concurrent: int = Field(3, ge=1, le=50)
    max_retries: int = Field(2, ge=0, le=10)
    timeout: float = Field(90, gt=0)
    num_evolutions: int = Field(1, ge=0, le=5)
    question_types: list[QuestionType] = Field(default_factory=lambda: list(QUESTION_TYPES))
    type_weights: dict[str, int] = Field(default_factory=dict)

    @field_validator("document_rules", "context_rules")
    @classmethod
    def check_rules(cls, targets):
        for target, rules in targets.items():
            if not target.strip() or target != target.strip():
                raise ValueError("规则的文件名或 context_id 不能为空，且不能带首尾空格")
            if any(not rule.strip() for rule in rules):
                raise ValueError(f"{target} 的规则不能为空；无需规则时请填写 []")
        return {target: [rule.strip() for rule in rules] for target, rules in targets.items()}

    @model_validator(mode="after")
    def check_types(self):
        if not self.question_types:
            raise ValueError("question_types cannot be empty")
        if any(k not in self.question_types or v < 1 for k, v in self.type_weights.items()):
            raise ValueError(
                "type_weights must use configured question_types with positive weights"
            )
        return self


class QualityConfig(StrictModel):
    overall_threshold: float = Field(0.8, ge=0, le=1)
    semantic_candidate_threshold: float = Field(0.85, ge=0, le=1)
    semantic_dedup: bool = True


class Config(StrictModel):
    input: str = "cairo_transportation"
    output: str = "data/output"
    mode: Literal["contexts", "docs"] = "contexts"
    language: str = "en"
    seed: int = 42
    chunking: ChunkConfig = Field(default_factory=ChunkConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    synthesis: SynthesisConfig = Field(default_factory=SynthesisConfig)
    quality: QualityConfig = Field(default_factory=QualityConfig)


def merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        result[key] = (
            merge(result[key], value)
            if isinstance(value, dict) and isinstance(result.get(key), dict)
            else value
        )
    return result


def load_environment():
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
    # The SDK treats an empty base URL as a literal URL, not as its default endpoint.
    if "OPENAI_BASE_URL" in os.environ and not os.environ["OPENAI_BASE_URL"].strip():
        del os.environ["OPENAI_BASE_URL"]


def load_config(path: Path | None = None) -> Config:
    load_environment()

    def read(file: Path, seen: set[Path]) -> dict:
        file = file.resolve()
        if file in seen:
            raise ValueError("Circular config inheritance")
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError("Config must be a YAML mapping")
        for section, key, env in MODEL_ENV:
            if isinstance(data.get(section), dict) and key in data[section]:
                raise ValueError(f"Set {env} in .env instead of YAML {section}.{key}")
        parent = data.pop("extends", None)
        return merge(read(file.parent / parent, seen | {file}), data) if parent else data

    config = Config.model_validate(read(path, set()) if path else {})
    return apply_model_environment(config)


MODEL_ENV = (
    ("synthesis", "model", "DEEPEVAL_MODEL"),
    ("synthesis", "judge_model", "DEEPEVAL_JUDGE_MODEL"),
    ("context", "embedding_model", "DEEPEVAL_EMBEDDING_MODEL"),
)


def apply_model_environment(config: Config) -> Config:
    # Resolved names are still serialized in batch snapshots, never credentials.
    for section, key, env in MODEL_ENV:
        if os.getenv(env):
            setattr(getattr(config, section), key, os.environ[env].strip())
    return config


def require_models(config: Config, *, generation=False, judge=False, embedding=False):
    required = (generation, judge, embedding)
    missing = [
        env
        for needed, (section, key, env) in zip(required, MODEL_ENV)
        if needed and not getattr(getattr(config, section), key).strip()
    ]
    if missing:
        raise ValueError("Configure model names in .env: " + ", ".join(missing))
