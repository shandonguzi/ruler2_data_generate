"""run_id、目录创建与 Markdown / metadata.jsonl 落盘。"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT
from .models import Document, RunInput
from .utils import make_doc_id, normalize_url

DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "data"
METADATA_FILENAME = "metadata.jsonl"
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_MAX_FILENAME_BYTES = 180


def build_metadata(document: Document) -> dict[str, Any]:
    extracted = document.extracted
    evaluation = document.evaluation
    relevance = evaluation.relevance
    authority = evaluation.authority
    domain = authority.domain
    page = authority.page
    richness = evaluation.content_richness
    quality = evaluation.content_quality

    return {
        "doc_id": extracted.doc_id,
        "url": extracted.url,
        "final_url": extracted.final_url,
        "title": extracted.title,
        "site": extracted.site,
        "published_at": extracted.published_at,
        "fetched_at": extracted.fetched_at,
        "lang": extracted.lang,
        "matched_queries": list(extracted.matched_queries),
        "domain_decision": domain.decision if domain else None,
        "domain_score": domain.domain_score if domain else None,
        "page_score": page.page_score if page else None,
        "relevance_score": relevance.relevance_score,
        "relevance_level": relevance.relevance_level,
        "content_type": relevance.content_type,
        "matched_scope": relevance.matched_scope,
        "location_match": relevance.location_match,
        "language_match": relevance.language_match,
        "temporal_match": relevance.temporal_match,
        "relevance_flags": list(relevance.flags),
        "relevance_reason": relevance.reason,
        "review_model": relevance.model,
        "review_reviewed_at": relevance.reviewed_at,
        "content_richness_score": richness.score,
        "content_richness_level": richness.level,
        "content_quality_score": quality.score,
        "content_quality_level": quality.level,
        "content_reason": richness.reason or quality.reason,
        "authority_score": authority.authority_score,
        "authority_level": authority.authority_level,
        "quality_flags": list(authority.quality_flags),
        "authority_reasons": list(authority.authority_reasons),
        "evaluation_decision": evaluation.decision,
        "evaluation_drop_reasons": list(evaluation.drop_reasons),
        "extractor": extracted.extractor,
    }


class RunWriter:
    def __init__(
        self,
        output_root: str | Path | None = None,
        run: RunInput | None = None,
        *,
        clock=datetime.now,
    ) -> None:
        if run is None:
            raise ValueError("RunWriter 需要 run 配置")
        self.run = run
        self.output_root = (
            Path(output_root) if output_root is not None else DEFAULT_OUTPUT_ROOT
        )
        self._clock = clock
        self._ids: dict[str, str] = {}
        self._filenames: set[str] = set()
        self._paths: dict[str, Path] = {}
        self._recorded: set[str] = set()
        self.run_id, self.run_dir = self._create_run_dir()
        self._metadata_path = self.run_dir / METADATA_FILENAME
        self._append_metadata(
            {
                "type": "run",
                "run_id": self.run_id,
                "s3": self.run.s3,
                "s4": self.run.s4,
                "location": self.run.location,
                "language": self.run.language,
                "time_range": self.run.time_range,
                "generated_at": self._generated_at,
            }
        )

    def _create_run_dir(self) -> tuple[str, Path]:
        now = self._clock()
        self._generated_at = now.isoformat()
        prefix = "-".join(
            part
            for part in (self.run.s3, self.run.s4, self.run.location, self.run.language)
            if part
        )
        stamp = now.strftime("%Y%m%d-%H%M%S")
        base = f"{_sanitize_filename(prefix, fallback='run')}-{stamp}"
        for suffix in range(1, 101):
            run_id = base if suffix == 1 else f"{base}-{suffix}"
            run_dir = self.output_root / run_id
            try:
                run_dir.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                continue
            return run_id, run_dir
        raise OSError("无法创建唯一的运行目录")

    def _allocate_filename(self, title: str, doc_id: str) -> str:
        base = _sanitize_filename(title, fallback=doc_id)
        candidate = base
        index = 1
        while True:
            filename = f"{candidate}.md"
            if filename not in self._filenames and not (self.run_dir / filename).exists():
                self._filenames.add(filename)
                return filename
            candidate = f"{base}({index})"
            index += 1

    def write(self, document: Document) -> Path:
        doc_id = document.extracted.doc_id
        url = document.extracted.url
        normalized = normalize_url(url)
        salt = 0
        while doc_id in self._ids and self._ids[doc_id] != url:
            salt += 1
            doc_id = make_doc_id(normalized, salt)
        self._ids[doc_id] = url
        document.extracted.doc_id = doc_id

        body = document.extracted.markdown.strip() + "\n"

        existing = self._paths.get(doc_id)
        if existing is None:
            name = self._allocate_filename(document.extracted.title, doc_id)
            path = self.run_dir / name
            self._paths[doc_id] = path
        else:
            path = existing
        _atomic_write(path, body)

        if doc_id not in self._recorded:
            self._recorded.add(doc_id)
            record = build_metadata(document)
            record["file"] = path.name
            self._append_metadata({"type": "document", **record})
        return path

    def _append_metadata(self, record: dict[str, Any]) -> None:
        """追加一行 JSON；单次 write 避免行内容与其他写入交叉。"""
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._metadata_path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def write_all(self, documents: list[Document]) -> list[Path]:
        return [self.write(document) for document in documents]


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _sanitize_filename(title: str, *, fallback: str) -> str:
    """把页面标题转成安全的文件名主体；空标题退回 doc_id。"""
    text = (title or "").strip()
    text = _INVALID_FILENAME_CHARS.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = fallback
    encoded = text.encode("utf-8")
    if len(encoded) > _MAX_FILENAME_BYTES:
        text = encoded[:_MAX_FILENAME_BYTES].decode("utf-8", "ignore").strip(" .")
    return text or fallback

