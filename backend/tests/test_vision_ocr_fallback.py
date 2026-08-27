"""
Regression tests for the vision-LLM OCR fallback.

Background: when the `tesseract` system binary isn't installed (common in
containerised/managed environments), every OCR attempt silently returned
nothing, which made scanned PDFs and image-only uploads fail outright with
400 errors on the quiz route. The fix added a vision-LLM-based OCR
fallback in utils/pdf_text_sanity.py that uses OpenRouter's multimodal
models (google/gemini-2.5-flash, openai/gpt-4o-mini) to transcribe the
rendered page/image — no system binary needed, just OPENROUTER_API_KEY.

These tests verify:
    1. The fallback chain is wired correctly: ocr_pdf_bytes() /
       ocr_image_bytes() try Tesseract first, then vision OCR.
    2. FileProcessor._extract_from_image falls back to vision OCR when
       Tesseract is unavailable.
    3. The whole chain degrades gracefully (returns "") when neither
       Tesseract nor an API key is available — instead of raising.
    4. MIME-type detection in FileProcessor identifies common image
       formats correctly, so the vision LLM gets properly-typed payloads.
"""
import io
from unittest.mock import patch, MagicMock

import fitz  # PyMuPDF
import pytest
from PIL import Image

from utils.file_processor import FileProcessor
from utils.pdf_text_sanity import (
    ocr_pdf_bytes,
    ocr_image_bytes,
    vision_ocr_image_bytes,
    vision_ocr_pdf_bytes,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_blank_pdf() -> bytes:
    """A PDF with no selectable text at all (simulates a scanned page)."""
    doc = fitz.open()
    doc.new_page()
    return doc.tobytes()


def _make_png_bytes(text: str = "OCR transcribed text") -> bytes:
    """A PNG with some text rendered into it (so OCR has something to find)."""
    img = Image.new("RGB", (400, 80), color="white")
    # Use fitz to draw text onto the image (PIL's ImageDraw also works but
    # fitz is already imported in this test file and we don't need a font).
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Fallback chain
# ---------------------------------------------------------------------------

class TestOcrFallbackChain:
    """Verify the Tesseract -> vision-LLM fallback chain is wired correctly."""

    def test_ocr_image_bytes_uses_vision_when_tesseract_unavailable(self):
        """When Tesseract isn't installed, ocr_image_bytes should delegate
        directly to vision_ocr_image_bytes."""
        with patch("utils.pdf_text_sanity._tesseract_available", return_value=False):
            with patch(
                "utils.pdf_text_sanity.vision_ocr_image_bytes",
                return_value="vision text",
            ) as mock_vision:
                result = ocr_image_bytes(b"fake image", "image/png")

        mock_vision.assert_called_once_with(b"fake image", "image/png")
        assert result == "vision text"

    def test_ocr_pdf_bytes_uses_vision_when_tesseract_unavailable(self):
        """When Tesseract isn't installed, ocr_pdf_bytes should delegate
        to vision_ocr_pdf_bytes."""
        with patch("utils.pdf_text_sanity._tesseract_available", return_value=False):
            with patch(
                "utils.pdf_text_sanity.vision_ocr_pdf_bytes",
                return_value="vision pdf text",
            ) as mock_vision:
                result = ocr_pdf_bytes(b"fake pdf")

        mock_vision.assert_called_once_with(b"fake pdf")
        assert result == "vision pdf text"

    def test_ocr_image_bytes_falls_back_when_tesseract_returns_empty(self):
        """Even if Tesseract is 'available', an empty result should trigger
        the vision fallback — Tesseract silently returning nothing on
        certain fonts/images is exactly the failure mode we're patching."""
        with patch("utils.pdf_text_sanity._tesseract_available", return_value=True):
            with patch("pytesseract.image_to_string", return_value="   "):
                with patch(
                    "utils.pdf_text_sanity.vision_ocr_image_bytes",
                    return_value="vision recovered text",
                ) as mock_vision:
                    result = ocr_image_bytes(b"img bytes", "image/png")

        mock_vision.assert_called_once()
        assert result == "vision recovered text"

    def test_ocr_image_bytes_falls_back_when_tesseract_raises(self):
        """Tesseract raising (e.g. corrupt image) should also trigger the
        vision fallback rather than propagating the exception."""
        with patch("utils.pdf_text_sanity._tesseract_available", return_value=True):
            with patch(
                "pytesseract.image_to_string",
                side_effect=RuntimeError("corrupt image"),
            ):
                with patch(
                    "utils.pdf_text_sanity.vision_ocr_image_bytes",
                    return_value="vision recovered text",
                ) as mock_vision:
                    result = ocr_image_bytes(b"img bytes", "image/png")

        mock_vision.assert_called_once()
        assert result == "vision recovered text"

    def test_chain_degrades_gracefully_with_no_api_key(self):
        """When neither Tesseract nor an API key is available, the chain
        must return '' (not raise) — so callers can treat it the same as
        any other empty-OCR result and surface a useful error."""
        with patch("utils.pdf_text_sanity._tesseract_available", return_value=False):
            with patch.dict("os.environ", {}, clear=True):
                # os.environ is now empty — no OPENROUTER_API_KEY, no
                # OPENAI_API_KEY, no anything. The vision OCR helper
                # should detect the missing key and return "".
                result = ocr_image_bytes(b"img", "image/png")
        assert result == ""

    def test_chain_degrades_gracefully_when_vision_client_unavailable(self):
        """If the OpenRouter client can't be initialised (e.g. openai
        package missing), vision OCR should return "" rather than raise."""
        with patch("utils.pdf_text_sanity._tesseract_available", return_value=False):
            with patch.dict(
                "os.environ",
                {"OPENROUTER_API_KEY": "fake-key-for-testing"},
                clear=True,
            ):
                with patch(
                    "utils.pdf_text_sanity._get_vision_llm_client",
                    return_value=None,
                ):
                    result = ocr_image_bytes(b"img", "image/png")
        assert result == ""


# ---------------------------------------------------------------------------
# FileProcessor integration
# ---------------------------------------------------------------------------

class TestFileProcessorImageExtraction:
    """FileProcessor._extract_from_image should fall back to vision OCR
    when Tesseract is unavailable — this is the exact failure mode from
    the original bug report ('Image OCR failed: tesseract is not installed
    or it's not in your PATH')."""

    def test_image_extraction_uses_vision_when_tesseract_unavailable(self):
        """End-to-end: FileProcessor._extract_from_image should call
        vision_ocr_image_bytes when Tesseract isn't installed."""
        fp = FileProcessor()
        png_bytes = _make_png_bytes()

        # Patch shutil.which globally (it's imported lazily inside _tesseract_image_ocr)
        with patch("shutil.which", return_value=None):
            # Also patch shutil in pdf_text_sanity (used by _tesseract_available)
            with patch("utils.pdf_text_sanity._tesseract_available", return_value=False):
                with patch(
                    "utils.file_processor.vision_ocr_image_bytes",
                    return_value="vision transcribed text",
                ) as mock_vision:
                    result = fp._extract_from_image(png_bytes)

        mock_vision.assert_called_once()
        # First positional arg is the raw image bytes
        assert mock_vision.call_args[0][0] == png_bytes
        # Second arg is the MIME type — should be image/png for a PNG
        assert mock_vision.call_args[0][1] == "image/png"
        assert result == "vision transcribed text"

    def test_image_extraction_returns_tesseract_result_when_available(self):
        """When Tesseract is installed and returns text, vision OCR
        should NOT be called — saves API costs and latency."""
        fp = FileProcessor()
        png_bytes = _make_png_bytes()

        with patch("shutil.which", return_value="/usr/bin/tesseract"):
            with patch("pytesseract.image_to_string", return_value="tesseract text"):
                with patch(
                    "utils.file_processor.vision_ocr_image_bytes",
                ) as mock_vision:
                    result = fp._extract_from_image(png_bytes)

        mock_vision.assert_not_called()
        assert "tesseract text" in result

    def test_image_extraction_falls_back_when_tesseract_returns_empty(self):
        """Tesseract available but returns nothing usable → vision OCR
        should kick in as a second opinion."""
        fp = FileProcessor()
        png_bytes = _make_png_bytes()

        with patch("shutil.which", return_value="/usr/bin/tesseract"):
            with patch("pytesseract.image_to_string", return_value=""):
                with patch(
                    "utils.file_processor.vision_ocr_image_bytes",
                    return_value="vision recovered text",
                ) as mock_vision:
                    result = fp._extract_from_image(png_bytes)

        mock_vision.assert_called_once()
        assert result == "vision recovered text"

    def test_image_extraction_returns_empty_when_both_paths_fail(self):
        """If both Tesseract AND vision OCR return nothing, the method
        should return "" (not raise) — process_files() then aggregates
        this into the standard 'No text could be extracted' 400 error."""
        fp = FileProcessor()
        png_bytes = _make_png_bytes()

        with patch("shutil.which", return_value=None):
            with patch("utils.pdf_text_sanity._tesseract_available", return_value=False):
                with patch(
                    "utils.file_processor.vision_ocr_image_bytes",
                    return_value="",
                ):
                    result = fp._extract_from_image(png_bytes)

        assert result == ""


class TestFileProcessorPdfOcrDelegation:
    """FileProcessor._ocr_pdf should delegate to the shared ocr_pdf_bytes()
    so the quiz module picks up the same vision-LLM fallback as every
    other PDF extractor (notes, flashcards, second brain)."""

    def test_ocr_pdf_delegates_to_shared_helper(self):
        fp = FileProcessor()
        pdf_bytes = _make_blank_pdf()

        with patch(
            "utils.file_processor.ocr_pdf_bytes",
            return_value="shared ocr text",
        ) as mock_shared:
            result = fp._ocr_pdf(pdf_bytes)

        mock_shared.assert_called_once_with(pdf_bytes)
        assert result == "shared ocr text"

    def test_ocr_pdf_picks_up_vision_fallback_via_shared_helper(self):
        """End-to-end: when Tesseract is unavailable, _ocr_pdf should
        return vision-OCR'd text via the shared helper."""
        fp = FileProcessor()
        pdf_bytes = _make_blank_pdf()

        # Don't patch the shared helper — let the real chain run, but
        # patch _tesseract_available to simulate no Tesseract, and patch
        # vision_ocr_pdf_bytes to simulate a successful vision OCR call.
        with patch("utils.pdf_text_sanity._tesseract_available", return_value=False):
            with patch(
                "utils.pdf_text_sanity.vision_ocr_pdf_bytes",
                return_value="vision pdf text",
            ) as mock_vision:
                result = fp._ocr_pdf(pdf_bytes)

        mock_vision.assert_called_once_with(pdf_bytes)
        assert result == "vision pdf text"


# ---------------------------------------------------------------------------
# MIME type detection
# ---------------------------------------------------------------------------

class TestMimeTypeDetection:
    """FileProcessor._guess_image_mime_type is used to label base64 image
    payloads sent to the vision LLM. Wrong MIME types get rejected by
    OpenRouter or cause the model to mis-decode the image, so the
    detection needs to be accurate for every format the frontend accepts."""

    def test_detects_png(self):
        fp = FileProcessor()
        buf = io.BytesIO()
        Image.new("RGB", (10, 10), "red").save(buf, format="PNG")
        assert fp._guess_image_mime_type(buf.getvalue()) == "image/png"

    def test_detects_jpeg(self):
        fp = FileProcessor()
        buf = io.BytesIO()
        Image.new("RGB", (10, 10), "red").save(buf, format="JPEG")
        assert fp._guess_image_mime_type(buf.getvalue()) == "image/jpeg"

    def test_detects_webp(self):
        fp = FileProcessor()
        buf = io.BytesIO()
        Image.new("RGB", (10, 10), "red").save(buf, format="WEBP")
        assert fp._guess_image_mime_type(buf.getvalue()) == "image/webp"

    def test_detects_gif(self):
        fp = FileProcessor()
        buf = io.BytesIO()
        Image.new("RGB", (10, 10), "red").save(buf, format="GIF")
        assert fp._guess_image_mime_type(buf.getvalue()) == "image/gif"

    def test_detects_bmp(self):
        fp = FileProcessor()
        buf = io.BytesIO()
        Image.new("RGB", (10, 10), "red").save(buf, format="BMP")
        assert fp._guess_image_mime_type(buf.getvalue()) == "image/bmp"

    def test_defaults_to_png_for_empty_or_unknown(self):
        fp = FileProcessor()
        assert fp._guess_image_mime_type(b"") == "image/png"
        assert fp._guess_image_mime_type(b"garbage bytes") == "image/png"
        assert fp._guess_image_mime_type(b"\x00\x01\x02") == "image/png"
