from __future__ import annotations

import re
from functools import lru_cache

from .config import ChunkConfig
from .schema import Chunk, Document, digest, stable_id


@lru_cache(maxsize=1)
def encoding():
    import tiktoken

    return tiktoken.get_encoding("cl100k_base")


def tokens(text: str) -> int:
    return len(encoding().encode(text, disallowed_special=()))


def is_heading(line: str) -> bool:
    s = line.strip()
    return bool(
        s
        and (
            s.startswith("#")
            or re.match(r"^\d+\. [A-Z]", s)
            or re.match(r"^LINE \d+:", s)
            or (len(s) < 100 and s.isupper())
        )
    )


def is_caveat(line: str) -> bool:
    return bool(
        re.search(
            r"not a real.time|not independently|not.*verified|not.*sequence|not.*consistent|do not add|does not|without specifying|not explicitly|may have changed|check current|under construction|proposed|appears twice|do not match",
            line,
            re.I,
        )
    )


def chunk_document(doc: Document, config: ChunkConfig) -> list[Chunk]:
    lines = []
    offset = 0
    for line in doc.text.splitlines(keepends=True):
        lines.append((offset, offset + len(line), line))
        offset += len(line)
    chunks = []
    section = [doc.path.rsplit("/", 1)[-1]]

    def add(start: int, end: int, kind="text", header=None):
        # Trim only the boundaries; every quote remains a real contiguous source span.
        while start < end and doc.text[start].isspace():
            start += 1
        while end > start and doc.text[end - 1].isspace():
            end -= 1
        if start == end:
            return
        spans = [p["page"] for p in doc.page_spans if p["end"] > start and p["start"] < end]
        text = doc.text[start:end]
        chunks.append(
            Chunk(
                chunk_id=stable_id("chunk", doc.document_id, start, end, kind),
                document_id=doc.document_id,
                text=text,
                section_path=list(section),
                char_start=start,
                char_end=end,
                page_start=min(spans) if spans else None,
                page_end=max(spans) if spans else None,
                content_hash=digest(text),
                language=doc.language,
                kind=kind,
                table_header=header,
            )
        )

    i = 0
    while i < len(lines):
        start, end, line = lines[i]
        if not line.strip():
            i += 1
            continue
        if is_heading(line):
            section = [section[0], line.strip().lstrip("# ")]
        if "|" in line:
            header = line.strip()
            i += 1
            table_start, table_end = start, end
            while i < len(lines) and "|" in lines[i][2]:
                row_start, row_end, row = lines[i]
                if (
                    tokens(doc.text[table_start:row_end]) + tokens(header) > config.size
                    and table_end > table_start
                ):
                    add(table_start, table_end, "table", header)
                    table_start = row_start
                table_end = row_end
                i += 1
            add(table_start, table_end, "table", header)
            continue
        # Keep ordinary paragraphs/lines together, but never consume a section/table boundary.
        i += 1
        while i < len(lines):
            next_start, next_end, next_line = lines[i]
            if not next_line.strip() or is_heading(next_line) or "|" in next_line:
                break
            if tokens(doc.text[start:next_end]) > config.size:
                break
            end = next_end
            i += 1
        text = doc.text[start:end]
        if tokens(text) <= config.size:
            add(start, end)
        else:
            # Token byte offsets avoid corrupting multibyte characters (e.g. Chinese).
            token_ids = encoding().encode(text, disallowed_special=())
            boundaries = [0]
            for token in token_ids:
                boundaries.append(boundaries[-1] + len(encoding().decode_single_token_bytes(token)))
            byte_text = text.encode("utf-8")
            for t in range(0, len(token_ids), config.size - config.overlap):
                lo, hi = boundaries[t], boundaries[min(t + config.size, len(token_ids))]
                while lo < len(byte_text) and byte_text[lo] & 0xC0 == 0x80:
                    lo += 1
                while hi < len(byte_text) and byte_text[hi] & 0xC0 == 0x80:
                    hi += 1
                a = len(byte_text[:lo].decode("utf-8"))
                b = len(byte_text[:hi].decode("utf-8"))
                add(start + a, start + b)
                if hi == len(byte_text):
                    break
        # Caveats are independent support spans, even when embedded in a long paragraph.
    for start, end, line in lines:
        if is_caveat(line):
            owner = next((c for c in chunks if c.char_start <= start < c.char_end), None)
            if owner:
                section = owner.section_path
            add(start, end, "caveat")
    return chunks
