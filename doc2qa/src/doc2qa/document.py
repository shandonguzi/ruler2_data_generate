from __future__ import annotations

import re
from pathlib import Path

from .schema import Document, digest, stable_id

SUPPORTED = {".txt", ".md", ".markdown", ".mdx", ".pdf", ".docx"}


def normalize(text: str) -> str:
    # Preserve whitespace, tables, numbers and qualifiers. Offsets refer to this snapshot.
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")


def parse(path: Path, language: str) -> Document:
    raw = path.read_bytes()
    suffix = path.suffix.lower()
    pages = []
    if suffix in {".txt", ".md", ".markdown", ".mdx"}:
        text = normalize(raw.decode("utf-8-sig", errors="strict"))
    elif suffix == ".pdf":
        from pypdf import PdfReader

        parts = []
        offset = 0
        for page_no, page in enumerate(PdfReader(path).pages, 1):
            part = normalize(page.extract_text() or "")
            if not part.strip():
                raise ValueError(f"{path}: page {page_no} has no text; OCR is required")
            pages.append({"page": page_no, "start": offset, "end": offset + len(part)})
            parts.append(part)
            offset += len(part) + 2
        text = "\n\n".join(parts)
    elif suffix == ".docx":
        from docx import Document as WordDocument
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        parts = []
        for item in WordDocument(path).iter_inner_content():
            if isinstance(item, Table):
                parts.append(
                    "\n".join(" | ".join(cell.text for cell in row.cells) for row in item.rows)
                )
            elif isinstance(item, Paragraph):
                heading = item.style.name.startswith("Heading") if item.style else False
                parts.append(("## " if heading else "") + item.text)
        text = normalize("\n\n".join(parts))
    else:
        raise ValueError(f"Unsupported input: {path}")
    if not text.strip():
        raise ValueError(f"Empty document: {path}")
    return Document(
        document_id=stable_id("doc", digest(raw)),
        path=str(path.resolve()),
        file_sha256=digest(raw),
        size_bytes=len(raw),
        format=suffix[1:],
        text=text,
        language=language,
        page_spans=pages,
        source_urls=list(dict.fromkeys(re.findall(r"https?://[^\s)]+", text))),
        compiled_date=(
            re.search(r"compiled ([^\n]+)", text).group(1)
            if re.search(r"compiled ([^\n]+)", text)
            else None
        ),
    )


def ingest(source: Path, language: str) -> list[Document]:
    if not source.exists():
        raise ValueError(f"Input does not exist: {source}")
    files = (
        [source]
        if source.is_file()
        else sorted(p for p in source.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED)
    )
    if not files:
        raise ValueError(f"No supported documents in {source}")
    by_hash = {}
    for path in files:
        doc = parse(path, language)
        if doc.file_sha256 in by_hash:
            by_hash[doc.file_sha256].aliases.append(doc.path)
        else:
            by_hash[doc.file_sha256] = doc
    return list(by_hash.values())
