"""
Regression tests for magic-byte sniffing in FileProcessor._extract.

Background: when files are dragged from the folder tree (workspace files)
to the quiz generator, the frontend's resolveDropFileName can rename a
.pdf to .txt (e.g. when the file list response includes a stale text
column from a previous broken extraction). Without magic-byte sniffing,
the backend's _extract() routes purely by filename extension, so a .txt
file goes through content.decode("utf-8", errors="ignore") — completely
bypassing _extract_from_pdf + fitz + OCR + safe_strip_leaked_pdf_objects.
The decoded "text" is actually binary PDF bytes mangled into UTF-8,
which contains tons of /Type, /Catalog, endobj tokens. looks_like_garbage()
correctly flags it → 400 "PDF encoding data" error.

The fix adds magic-byte sniffing at the top of _extract(): if the content
starts with %PDF-, route through _extract_from_pdf regardless of the
filename extension. Same for ZIP-based formats (DOCX/XLSX/PPTX) via PK\x03\x04.

These tests verify:
    1. A .txt-labeled file with PDF magic bytes is extracted as a PDF.
    2. A .txt-labeled file with real text content is still extracted as text.
    3. A .txt-labeled file with ZIP magic bytes tries DOCX/XLSX/PPTX extractors.
    4. A correctly-named .pdf file is not double-sniffed (happy path).
    5. Empty content doesn't crash the sniffer.
"""
import io
from unittest.mock import patch, MagicMock

import fitz  # PyMuPDF
import pytest
from PIL import Image

from utils.file_processor import FileProcessor
from utils.pdf_text_sanity import looks_like_garbage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_real_pdf(text: str = "This is real PDF text about mitochondria.") -> bytes:
    """Build a real, minimal single-page PDF containing the given text."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    return doc.tobytes()


def _make_real_docx(text: str = "This is real DOCX text.") -> bytes:
    """Build a real DOCX file with the given text."""
    import docx
    doc = docx.Document()
    doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _make_garbage_pdf_text() -> bytes:
    """Simulate what happens when raw PDF bytes are decoded as UTF-8 with
    errors='ignore' — produces a string full of /Type, /Catalog, endobj
    tokens that looks_like_garbage() correctly flags."""
    pdf_bytes = _make_real_pdf("test content")
    return pdf_bytes  # the raw bytes, not decoded


# ---------------------------------------------------------------------------
# PDF magic-byte sniffing
# ---------------------------------------------------------------------------

class TestPdfMagicByteSniffing:
    """Verify that a .txt-labeled file containing real PDF bytes is
    correctly routed through _extract_from_pdf instead of being decoded
    as UTF-8 text."""

    def test_txt_file_with_pdf_magic_is_extracted_as_pdf(self):
        """A file named .txt but starting with %PDF- should be extracted
        through _extract_from_pdf, not decoded as UTF-8."""
        fp = FileProcessor()
        pdf_bytes = _make_real_pdf("Mitochondria are the powerhouse of the cell.")

        # The bytes start with %PDF-
        assert pdf_bytes[:5] == b"%PDF-", "Test setup: bytes should start with PDF magic"

        result = fp._extract(pdf_bytes, "txt", "lecture.txt")

        # Should NOT be the garbage UTF-8 decode of the PDF bytes
        assert "Mitochondria" in result, f"PDF text not extracted, got: {result[:200]}"
        assert not looks_like_garbage(result), "Extracted text looks like garbage"
        # Should NOT contain PDF structural tokens
        assert "/Type" not in result
        assert "endobj" not in result

    def test_txt_file_with_real_text_is_still_extracted_as_text(self):
        """A real .txt file (no PDF magic) should still go through the
        normal text decode path — magic-byte sniffing must not break
        the happy path for actual text files."""
        fp = FileProcessor()
        text_bytes = b"Hello world, this is a real text file about biology."

        result = fp._extract(text_bytes, "txt", "notes.txt")

        assert result == "Hello world, this is a real text file about biology."

    def test_md_file_with_pdf_magic_is_extracted_as_pdf(self):
        """Same sniffing should apply to .md files — a markdown file
        that's actually a PDF should be extracted as PDF."""
        fp = FileProcessor()
        pdf_bytes = _make_real_pdf("Photosynthesis converts light into energy.")

        result = fp._extract(pdf_bytes, "md", "notes.md")

        assert "Photosynthesis" in result
        assert not looks_like_garbage(result)

    def test_pdf_file_is_not_double_sniffed(self):
        """A correctly-named .pdf file should go through _extract_from_pdf
        directly — the magic-byte check should be skipped (ext == 'pdf')."""
        fp = FileProcessor()
        pdf_bytes = _make_real_pdf("Real PDF content here.")

        with patch.object(fp, "_extract_from_pdf", wraps=fp._extract_from_pdf) as mock_pdf:
            result = fp._extract(pdf_bytes, "pdf", "document.pdf")

        mock_pdf.assert_called_once_with(pdf_bytes)
        assert "Real PDF content here." in result

    def test_empty_content_does_not_crash_sniffer(self):
        """Empty content (no magic bytes) should not crash the sniffer
        — it should fall through to the extension-based router and
        return empty string for unknown types."""
        fp = FileProcessor()
        result = fp._extract(b"", "txt", "empty.txt")
        assert result == ""

    def test_short_content_does_not_crash_sniffer(self):
        """Content shorter than the magic-byte length should not crash
        the sniffer (no IndexError on content[:5])."""
        fp = FileProcessor()
        result = fp._extract(b"hi", "txt", "short.txt")
        assert result == "hi"

    def test_sniffing_catches_the_folder_tree_bug_scenario(self):
        """End-to-end simulation of the folder-tree bug:
        - Frontend fetches real PDF bytes from workspace endpoint
        - Frontend's resolveDropFileName renames .pdf → .txt (because
          inline content was in the drag payload)
        - Backend receives a .txt file containing real PDF bytes
        - Without sniffing: decodes as UTF-8 → garbage → 400 error
        - With sniffing: routes through _extract_from_pdf → clean text
        """
        fp = FileProcessor()
        pdf_bytes = _make_real_pdf(
            "The cell membrane is a biological membrane that separates "
            "the interior of all cells from the outside environment."
        )

        # Simulate what the frontend sends: real PDF bytes, but .txt filename
        result = fp._extract(pdf_bytes, "txt", "Labsheet_MongoDB.txt")

        # The fix should produce clean text, not garbage
        assert "cell membrane" in result.lower() or "biological membrane" in result.lower()
        assert not looks_like_garbage(result), (
            f"Extracted text still looks like garbage — the magic-byte "
            f"sniffing fix is not working. First 200 chars: {result[:200]}"
        )


# ---------------------------------------------------------------------------
# ZIP magic-byte sniffing (DOCX/XLSX/PPTX)
# ---------------------------------------------------------------------------

class TestZipMagicByteSniffing:
    """Verify that a .txt-labeled file containing real DOCX/XLSX/PPTX
    bytes (which are ZIP archives starting with PK) is correctly routed
    through the appropriate extractor."""

    def test_txt_file_with_docx_magic_is_extracted_as_docx(self):
        """A file named .txt but containing a real DOCX (ZIP) should be
        extracted through _extract_from_docx."""
        fp = FileProcessor()
        docx_bytes = _make_real_docx("This is a DOCX paragraph about chemistry.")

        # DOCX files start with PK (ZIP magic)
        assert docx_bytes[:2] == b"PK", "Test setup: DOCX should start with PK"

        result = fp._extract(docx_bytes, "txt", "document.txt")

        assert "chemistry" in result.lower(), f"DOCX text not extracted, got: {result[:200]}"

    def test_txt_file_with_real_text_is_not_tried_as_zip(self):
        """A real .txt file should NOT trigger the ZIP sniffer — even
        if it happens to start with 'PK' (unlikely but possible in
        prose). The ZIP sniffer should only fire when the magic bytes
        match AND the content looks like a real ZIP archive."""
        fp = FileProcessor()
        # "PK" followed by non-ZIP content — this is valid text that
        # starts with "PK" but is not a ZIP file
        text_bytes = b"PK stands for Pakistan in this text file."

        result = fp._extract(text_bytes, "txt", "countries.txt")

        # Should be decoded as text, not tried as DOCX
        assert result == "PK stands for Pakistan in this text file."
