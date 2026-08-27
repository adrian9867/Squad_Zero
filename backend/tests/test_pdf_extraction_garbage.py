"""
Regression tests for the PDF text-extraction fix.

Previously, PDF extraction relied on parsers (PyPDF2, or raw fitz
get_text() with no sanity check) which on certain PDFs (unusual font
encodings, XObject-heavy layouts, some generators, slightly malformed
xref tables) leaked raw internal object-dictionary keys (/Type,
/Catalog, /Font, endobj, xref, ...) into the "extracted" text instead of
raising an error. That garbage text still had plenty of words, so it
sailed past word-count guards and got fed straight to the AI (quiz
generator, note builder, etc.), producing meaningless questions/notes
about the PDF's internal structure instead of its actual subject matter.

Note: this codebase has several independent PDF extraction
implementations (quiz upload via utils/file_processor.py, structured
notes via m3_structurednotes/services.py, flashcards/files/pdf routes
via app/services/pdf_reader.py, mindmap generation via
services/pdf_service.py, second brain via second_brain/extract.py).
The garbage-detection heuristic and OCR fallback are centralized in
utils/pdf_text_sanity.py so all of them share one fix instead of each
needing (and risking missing) its own copy.

These tests cover:
    1. The shared looks_like_garbage() heuristic correctly distinguishes
       leaked PDF structure from real prose.
    2. FileProcessor._extract_from_pdf falls back to OCR when the primary
       extraction result looks like garbage.
    3. A normal, real-text PDF still extracts cleanly via the fitz path
       (no unnecessary OCR fallback).
"""
import io
from unittest.mock import patch

import fitz  # PyMuPDF
import pytest
from PIL import Image

from utils.file_processor import FileProcessor
from utils.pdf_text_sanity import (
    looks_like_garbage as shared_looks_like_garbage,
    strip_leaked_pdf_objects,
    safe_strip_leaked_pdf_objects,
)

# Raw PDF-object-dictionary text, the same shape PyPDF2 was observed to
# leak on problematic PDFs.
GARBAGE_TEXT = """1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] >> endobj
3 0 obj << /Type /Page /Font /F1 12 0 R /ExtGState << /GS1 13 0 R >> >> endobj
xref
0 4
trailer << /Size 4 /Root 1 0 R >>
startxref
stream endstream"""

REAL_PROSE = """Mitochondria are membrane-bound organelles found in most eukaryotic
cells. They generate most of the cell's chemical energy needed to power
biochemical reactions. This process, called cellular respiration,
produces ATP as the primary energy currency of the cell."""


def _make_text_pdf(text: str) -> bytes:
    """Build a real, minimal single-page PDF containing the given text."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    return doc.tobytes()


def _make_blank_pdf() -> bytes:
    """A PDF with no selectable text at all (simulates a scanned page)."""
    doc = fitz.open()
    doc.new_page()
    return doc.tobytes()


class TestLooksLikeGarbage:
    def test_flags_pdf_structural_leakage(self):
        fp = FileProcessor()
        assert fp._looks_like_garbage(GARBAGE_TEXT) is True

    def test_does_not_flag_real_prose(self):
        fp = FileProcessor()
        assert fp._looks_like_garbage(REAL_PROSE) is False

    def test_does_not_flag_empty_or_tiny_text(self):
        fp = FileProcessor()
        assert fp._looks_like_garbage("") is False
        assert fp._looks_like_garbage("   ") is False
        assert fp._looks_like_garbage("a b c") is False

    def test_flags_low_word_like_ratio_even_without_exact_keywords(self):
        fp = FileProcessor()
        # No literal "/Type" or "endobj", but still symbol/number soup
        # rather than real words.
        noise = "3 0 R 12 0 R << >> [3 0 R] 0000000010 00000 n"
        assert fp._looks_like_garbage(noise) is True


class TestSharedModuleIsUsedConsistently:
    """FileProcessor delegates to the shared module rather than keeping its
    own copy of the heuristic — this is what lets every other extractor
    (notes, flashcards, mindmap, second brain) share the same fix instead
    of only one call site getting patched."""

    def test_file_processor_delegates_to_shared_helper(self):
        fp = FileProcessor()
        assert fp._looks_like_garbage(GARBAGE_TEXT) == shared_looks_like_garbage(GARBAGE_TEXT)
        assert fp._looks_like_garbage(REAL_PROSE) == shared_looks_like_garbage(REAL_PROSE)

    def test_other_extraction_sites_import_shared_helper(self):
        """Guards against a future edit silently reintroducing a local,
        unpatched copy of the heuristic in one of the other extractors."""
        import ast

        sites = [
            "app/services/pdf_reader.py",
            "services/pdf_service.py",
            "second_brain/extract.py",
            "m3_structurednotes/services.py",
        ]
        for path in sites:
            src = open(path).read()
            assert "pdf_text_sanity" in src, f"{path} no longer uses the shared sanity-check module"
            ast.parse(src)  # sanity: still valid Python


class TestStripLeakedPdfObjects:
    """Regression coverage for the surgical-strip fix: a small leaked PDF
    object block embedded inside an otherwise normal, realistically long
    document dilutes to near-zero density and correctly does NOT trip
    looks_like_garbage() (which judges the whole document, for deciding
    whether to trigger OCR) -- but it must still be removed by
    strip_leaked_pdf_objects(), which is applied unconditionally, at the
    very end, regardless of whether OCR fired for that document."""

    def test_removes_small_embedded_leak_too_diluted_to_flag_whole_document(self):
        long_prose = REAL_PROSE * 25
        small_leak = "/Type /Font /Subtype /Type1 /BaseFont /Arial"
        doc = long_prose + "\n" + small_leak + "\n" + long_prose

        # Confirms this really is the dilution scenario: the whole-document
        # check legitimately does not (and should not) flag it.
        assert shared_looks_like_garbage(doc) is False

        cleaned = strip_leaked_pdf_objects(doc)
        assert "/Type" not in cleaned
        assert cleaned.count("Mitochondria") == doc.count("Mitochondria")

    def test_removes_full_obj_xref_trailer_block(self):
        doc = "Intro paragraph.\n" + GARBAGE_TEXT + "\nConclusion paragraph."
        cleaned = strip_leaked_pdf_objects(doc)
        for token in ("endobj", "xref", "trailer", "startxref", "/Type"):
            assert token not in cleaned
        assert "Intro paragraph." in cleaned
        assert "Conclusion paragraph." in cleaned

    def test_leaves_real_prose_completely_unaffected(self):
        assert strip_leaked_pdf_objects(REAL_PROSE).strip() == REAL_PROSE.strip()

    def test_does_not_strip_ambiguous_ordinary_words(self):
        # "stream", "object", "trailer" are ordinary English words and must
        # never be treated as leaks on their own -- only the bracket-form
        # keys (/Type, /Font, ...) and unambiguous markers (endobj, xref,
        # startxref, endstream) are trusted on a single occurrence; bare
        # "obj"/"stream"/"trailer" are only removed when paired with their
        # closing marker via the block regexes.
        text = "The data stream from the river was the object of the study. His trailer was parked nearby."
        assert strip_leaked_pdf_objects(text) == text

    def test_handles_empty_and_none_gracefully(self):
        assert strip_leaked_pdf_objects("") == ""
        assert strip_leaked_pdf_objects(None) is None


class TestSafeStripLeakedPdfObjects:
    """safe_strip_leaked_pdf_objects() is what every extraction site
    actually calls: same surgical stripping, but falls back to the
    original text if stripping would hollow a non-empty input out to
    nothing -- preserving the existing "garbage pass-through beats an
    empty result" safety net for documents that are entirely leaked
    structure with no real content at all."""

    def test_behaves_like_strip_leaked_pdf_objects_on_normal_input(self):
        doc = "Intro paragraph.\n" + GARBAGE_TEXT + "\nConclusion paragraph."
        assert safe_strip_leaked_pdf_objects(doc) == strip_leaked_pdf_objects(doc)

    def test_falls_back_to_original_text_when_stripping_would_empty_it_out(self):
        # GARBAGE_TEXT alone is nothing *but* leaked structure -- stripping
        # it fully empties the string. The safe wrapper must preserve the
        # original text instead of returning "".
        assert strip_leaked_pdf_objects(GARBAGE_TEXT).strip() == ""
        assert safe_strip_leaked_pdf_objects(GARBAGE_TEXT) == GARBAGE_TEXT

    def test_handles_empty_and_none_gracefully(self):
        assert safe_strip_leaked_pdf_objects("") == ""
        assert safe_strip_leaked_pdf_objects(None) is None


class TestExtractFromPdf:
    def test_falls_back_to_ocr_when_extracted_text_looks_like_garbage(self):
        fp = FileProcessor()
        pdf_bytes = _make_text_pdf(REAL_PROSE)

        # Simulate a broken/leaky primary extraction path returning
        # PDF-structure garbage regardless of what's really on the page.
        with patch("fitz.open") as mock_fitz_open:
            mock_doc = mock_fitz_open.return_value
            mock_page = mock_doc.__iter__.return_value = iter([])
            # Make the fitz path itself return garbage text
            class _FakePage:
                def get_text(self):
                    return GARBAGE_TEXT
            mock_fitz_open.return_value = [_FakePage()]

            with patch.object(fp, "_ocr_pdf", return_value=REAL_PROSE) as mock_ocr:
                result = fp._extract_from_pdf(pdf_bytes)

        mock_ocr.assert_called_once()
        assert result == REAL_PROSE
        assert "endobj" not in result
        assert "/Type" not in result

    def test_keeps_garbage_text_only_if_ocr_also_yields_nothing(self):
        """If OCR fallback fails to produce anything, don't discard the
        only text we have -- better a garbage-flagged pass-through than
        an empty result the caller treats as total extraction failure."""
        fp = FileProcessor()
        pdf_bytes = _make_text_pdf(REAL_PROSE)

        class _FakePage:
            def get_text(self):
                return GARBAGE_TEXT

        with patch("fitz.open", return_value=[_FakePage()]):
            with patch.object(fp, "_ocr_pdf", return_value=""):
                result = fp._extract_from_pdf(pdf_bytes)

        assert result == GARBAGE_TEXT

    def test_normal_text_pdf_extracts_cleanly_without_ocr_fallback(self):
        fp = FileProcessor()
        pdf_bytes = _make_text_pdf(REAL_PROSE)

        with patch.object(fp, "_ocr_pdf") as mock_ocr:
            result = fp._extract_from_pdf(pdf_bytes)

        mock_ocr.assert_not_called()
        assert "Mitochondria" in result
        assert not fp._looks_like_garbage(result)

    def test_strips_small_leak_embedded_in_normal_pdf_without_triggering_ocr(self):
        """The unconditional strip runs even when nothing about the
        document was garbage enough to trigger OCR -- a small leaked
        fragment sitting inside an otherwise normal page must still be
        removed from the final returned text."""
        fp = FileProcessor()
        long_prose = REAL_PROSE * 25
        small_leak = "/Type /Font /Subtype /Type1 /BaseFont /Arial"
        text_with_leak = long_prose + "\n" + small_leak + "\n" + long_prose
        pdf_bytes = _make_text_pdf(REAL_PROSE)  # bytes don't matter, fitz is mocked below

        class _FakePage:
            def get_text(self):
                return text_with_leak

        with patch("fitz.open", return_value=[_FakePage()]):
            with patch.object(fp, "_ocr_pdf") as mock_ocr:
                result = fp._extract_from_pdf(pdf_bytes)

        mock_ocr.assert_not_called()  # whole document correctly isn't flagged as garbage
        assert "/Type" not in result
        assert "Mitochondria" in result

    def test_blank_scanned_pdf_still_triggers_ocr(self):
        fp = FileProcessor()
        pdf_bytes = _make_blank_pdf()

        with patch.object(fp, "_ocr_pdf", return_value="ocr recovered text") as mock_ocr:
            result = fp._extract_from_pdf(pdf_bytes)

        mock_ocr.assert_called_once()
        assert result == "ocr recovered text"
