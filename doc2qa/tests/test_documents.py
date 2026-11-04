import pytest

from doc2qa.chunking import chunk_document
from doc2qa.config import ChunkConfig
from doc2qa.document import ingest, parse


def test_real_source_snapshots_and_offsets(bundle):
    docs, chunks, _ = bundle
    assert len(docs) == 5
    for chunk in chunks.values():
        doc = docs[chunk.document_id]
        assert doc.text[chunk.char_start : chunk.char_end] == chunk.text
        assert chunk.page_start is None
    railway = next(d for d in docs.values() if "railway" in d.path)
    assert "without specifying currency" in railway.text
    assert "Alexandria | 190 | 150" in railway.text


def test_duplicate_content_and_new_versions(tmp_path):
    (tmp_path / "one.txt").write_text("version one", encoding="utf-8")
    (tmp_path / "copy.txt").write_text("version one", encoding="utf-8")
    docs = ingest(tmp_path, "en")
    assert len(docs) == 1 and len(docs[0].aliases) == 1
    old_id = docs[0].document_id
    (tmp_path / "one.txt").write_text("version two", encoding="utf-8")
    assert len(ingest(tmp_path, "en")) == 2
    assert parse(tmp_path / "one.txt", "en").document_id != old_id


@pytest.mark.parametrize("raw", [b"", b"\xff\xfe\xff"])
def test_bad_inputs_fail(tmp_path, raw):
    path = tmp_path / "bad.txt"
    path.write_bytes(raw)
    with pytest.raises((ValueError, UnicodeError)):
        parse(path, "en")


def test_table_row_headers_and_unicode_splitting(tmp_path):
    table = "# Fares\nDestination | First | Second\n" + "\n".join(
        f"City {i} | {i + 100} | {i + 50}" for i in range(80)
    )
    path = tmp_path / "table.md"
    path.write_text(table + "\n\n# 中文\n" + "中文证据不能丢失。" * 150, encoding="utf-8")
    doc = parse(path, "zh")
    chunks = chunk_document(doc, ChunkConfig(size=64, overlap=8))
    tables = [c for c in chunks if c.kind == "table"]
    assert len(tables) > 1
    assert all(c.table_header == "Destination | First | Second" for c in tables)
    assert all(c.text == doc.text[c.char_start : c.char_end] for c in chunks)
    assert all("�" not in c.text for c in chunks)


def test_docx_preserves_body_table_order(tmp_path):
    from docx import Document

    doc = Document()
    doc.add_heading("Fare table", 1)
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "Place", "Fare"
    table.cell(1, 0).text, table.cell(1, 1).text = "Station", "20"
    doc.add_paragraph("Currency not specified.")
    path = tmp_path / "test.docx"
    doc.save(path)
    parsed = parse(path, "en")
    assert (
        parsed.text.index("## Fare table")
        < parsed.text.index("Station | 20")
        < parsed.text.index("Currency not specified")
    )


def test_scanned_pdf_is_explicit_error(tmp_path):
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    path = tmp_path / "scan.pdf"
    writer.write(path)
    with pytest.raises(ValueError, match="OCR"):
        parse(path, "en")
