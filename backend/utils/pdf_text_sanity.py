"""
Shared PDF-extraction sanity checking and OCR fallback.

The codebase has several independent PDF text extractors (quiz upload,
structured notes, flashcards/files/pdf routes, mindmap generation, second
brain). All of them are susceptible to the same failure mode: on certain
PDFs (unusual font encodings, XObject-heavy layouts, some PDF
generators/exporters, slightly malformed xref tables), the extraction
library returns text that isn't real page content but leaked internal PDF
object-dictionary structure (`/Type`, `/Catalog`, `/Font`, `endobj`,
`xref`, ...). That garbage still has plenty of words, so length-based
"did we get enough text" checks pass it through, and it ends up fed
straight to the AI as if it were real material — producing meaningless
questions/notes/etc. about the PDF's internal structure instead of its
actual subject matter.

This module centralizes the fix so every extractor can share it instead of
each accumulating its own copy (or, as happened before, only some of them
getting patched):

    looks_like_garbage(text)          -- heuristic sanity check (whole
                                          document — used to decide
                                          whether to trigger OCR)
    strip_leaked_pdf_objects(text)    -- surgical removal of leaked PDF
                                          object-dictionary blocks that
                                          are embedded inside otherwise
                                          normal document text
    ocr_pdf_bytes(content)            -- OCR fallback that sidesteps the
                                          PDF parser entirely, from raw
                                          bytes
    ocr_pdf_path(file_path)           -- same, from a file path
    ocr_image_bytes(content, mime)    -- OCR for an image (jpg/png/etc.)
    vision_ocr_image_bytes(...)       -- vision-LLM-only OCR (no Tesseract
                                          binary needed) — used as the
                                          automatic fallback when local
                                          Tesseract is unavailable
    vision_ocr_pdf_bytes(content)     -- same, for PDFs

Note the two checks solve different shapes of the same failure mode:
looks_like_garbage() looks at the *whole* text and is meant to catch
pages/documents that are wholesale garbage (nothing but leaked PDF
internals) so OCR can be triggered as a full replacement. But on a long,
otherwise-normal document, a small leaked object block gets diluted to
near-zero density across the full text and never trips that check — yet
it's still sitting there verbatim, ready to be picked up by anything
downstream (e.g. an AI quiz generator scanning for material). That's what
strip_leaked_pdf_objects() is for: it doesn't decide whether to reject
anything, it just surgically deletes recognizable leaked-object syntax
wherever it appears, leaving surrounding real prose untouched. It should
be run unconditionally, as the last step, on whatever text a caller is
about to return — regardless of whether looks_like_garbage()/OCR fired at
all for that document.

OCR FALLBACK CHAIN
------------------
Local Tesseract OCR requires the `tesseract` system binary to be installed
(`apt install tesseract-ocr` on Debian/Ubuntu, `brew install tesseract`
on macOS). On systems where it isn't installed — common in containerised
or managed environments — every Tesseract attempt silently returns
nothing, which makes scanned PDFs and image-only uploads fail outright
with 400 errors.

This codebase already integrates with OpenRouter (which exposes vision-
capable LLMs like google/gemini-2.5-flash and openai/gpt-4o-mini), so we
use that as an automatic fallback when local Tesseract is unavailable.
The vision LLM doesn't need any system binary — it just sends the rendered
page/image as base64 to the chat completions endpoint with a "transcribe
all visible text" system prompt. Callers should use ocr_pdf_bytes() /
ocr_image_bytes() (which auto-fallback); call vision_ocr_*() directly
only if you explicitly want to skip Tesseract.
"""
import re

# Raw PDF object-dictionary tokens that should never appear in real
# extracted prose. Their presence means the extractor leaked internal PDF
# structure instead of page content.
_PDF_STRUCTURAL_TOKENS = re.compile(
    r"\b(?:endobj|endstream|obj|xref|startxref|stream|trailer)\b"
    r"|/(?:Type|Catalog|Pages|Page|Font|ExtGState|Filter|FlateDecode|"
    r"MediaBox|Contents|Resources|Annots|Widget|XObject)\b"
)

# Some PDF extractors drop the slash from dictionary keys.  The resulting
# text is still recognizable as PDF internals (for example
# "Type Catalog Pages Font ExtGState") but the original slash-based check
# cannot see it.  The strong keys below are uncommon in normal prose; a
# cluster of them is a reliable signal that the text layer is corrupt.
_PDF_STRUCTURAL_WORDS = re.compile(
    r"\b(?:Catalog|Pages|ExtGState|FlateDecode|MediaBox|Contents|Resources|"
    r"Annots|Widget|XObject|startxref|endobj|endstream)\b",
    re.IGNORECASE,
)


def looks_like_garbage(text: str) -> bool:
    """Heuristic: does this look like leaked PDF structure rather than
    real document text?

    Triggers on either a high density of raw structural tokens, or a very
    low ratio of real dictionary-ish words to total tokens (which also
    catches leakage that doesn't hit the exact keys checked above).
    """
    if not text or not text.strip():
        return False

    tokens = text.split()
    if len(tokens) < 5:
        return False

    struct_hits = len(_PDF_STRUCTURAL_TOKENS.findall(text))
    if struct_hits >= 3 or (struct_hits / len(tokens)) > 0.05:
        return True

    slashless_hits = {match.group(0).lower() for match in _PDF_STRUCTURAL_WORDS.finditer(text)}
    if len(slashless_hits) >= 2:
        return True

    # Ratio of tokens that look like ordinary words (letters, reasonable
    # length) vs. total. PDF internals are dominated by short symbolic
    # tokens, numbers, and slash-prefixed names.
    word_like = sum(1 for t in tokens if re.fullmatch(r"[A-Za-z]{2,}", t))
    if word_like / len(tokens) < 0.4:
        return True

    return False


# Patterns for the recognizable leaked-object shapes we surgically strip.
# Each is bounded by tokens ("obj"/"endobj", "xref"/"%%EOF", "trailer",
# "stream"/"endstream") that essentially never occur together in real
# prose, so these are safe to remove even mid-document.
_OBJ_BLOCK_RE = re.compile(r"\b\d+\s+\d+\s+obj\b.*?\bendobj\b", re.DOTALL)
# Bounded to the actual xref-table shape (header line + fixed-width entry
# lines) rather than "xref ... until some later marker", so a document
# missing a "%%EOF" trailer (or one where %%EOF never appears at all)
# can't cause this to swallow unrelated real content all the way to the
# end of the string.
_XREF_BLOCK_RE = re.compile(
    r"\bxref\b[ \t]*\n(?:\d+[ \t]+\d+[ \t]*\n)?(?:\d{10}[ \t]+\d{5}[ \t]+[fn][ \t]*\n?)*"
)
_TRAILER_BLOCK_RE = re.compile(r"\btrailer\b\s*<<.*?>>", re.DOTALL)
_STREAM_BLOCK_RE = re.compile(r"\bstream\b.*?\bendstream\b", re.DOTALL)
_STARTXREF_RE = re.compile(r"\bstartxref\b\s*\d*")
_EOF_MARKER_RE = re.compile(r"%%EOF\b")

# Tokens safe to treat as "this line is leaked PDF structure" on their own,
# with no dominance ratio needed — unlike bare "obj"/"stream"/"trailer"
# (which can be ordinary English words and are only trusted when paired
# with their closing marker via the block regexes above), a bracket-form
# key like "/Type" or an unambiguous marker like "endobj"/"xref" essentially
# never appears in genuine prose.
_UNAMBIGUOUS_LEAK_RE = re.compile(
    r"\b(?:endobj|endstream|xref|startxref)\b"
    r"|/(?:Type|Pages|Page|Font|ExtGState|Filter|FlateDecode|"
    r"MediaBox|Contents|Resources|Annots|Widget|XObject)\b"
)

_SLASHLESS_LEAK_LINE_RE = re.compile(
    r"\b(?:Catalog|ExtGState|FlateDecode|MediaBox|Contents|Resources|"
    r"Annots|Widget|XObject)\b",
    re.IGNORECASE,
)


def strip_leaked_pdf_objects(text: str) -> str:
    """Surgically remove leaked PDF object-dictionary syntax embedded in
    otherwise normal text, without touching the surrounding real content.

    Unlike looks_like_garbage(), which judges the whole document, this
    targets and deletes the leaked block itself — it's the right tool for
    a small chunk of `/Type /Catalog ... endobj` noise sitting inside an
    otherwise-normal document, where whole-document density would dilute
    to near-zero and never trip the garbage check.

    Safe to call unconditionally on any extracted text — a document with
    no leaked structure passes through unchanged.
    """
    if not text:
        return text

    cleaned = text
    cleaned = _OBJ_BLOCK_RE.sub(" ", cleaned)
    cleaned = _XREF_BLOCK_RE.sub(" ", cleaned)
    cleaned = _TRAILER_BLOCK_RE.sub(" ", cleaned)
    cleaned = _STREAM_BLOCK_RE.sub(" ", cleaned)
    cleaned = _STARTXREF_RE.sub(" ", cleaned)
    cleaned = _EOF_MARKER_RE.sub(" ", cleaned)

    # Catch stray leftovers line-by-line (e.g. a lone "/Type /Page" whose
    # obj/endobj markers were themselves truncated, malformed, or never
    # present, so the block regexes above didn't have a clean pair to
    # match). A single hit is enough to drop the line: these are the
    # unambiguous tokens only, so a line that just mentions an ordinary
    # word like "stream" or "trailer" in real prose is never touched here.
    kept_lines = [
        line for line in cleaned.split("\n")
        if not _UNAMBIGUOUS_LEAK_RE.search(line)
        and len({match.group(0).lower() for match in _SLASHLESS_LEAK_LINE_RE.finditer(line)}) < 2
    ]
    cleaned = "\n".join(kept_lines)

    # Collapse whitespace left behind by removed blocks.
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r" {2,}", " ", cleaned)

    return cleaned.strip()


def safe_strip_leaked_pdf_objects(text: str) -> str:
    """strip_leaked_pdf_objects(), but never returns empty text for
    non-empty input.

    A document that is *entirely* leaked PDF structure (no real content
    at all) is exactly the case looks_like_garbage() already exists to
    catch and route to OCR. If OCR then fails to produce anything, the
    caller's existing fallback is to pass the garbage text through
    rather than return nothing (an empty result tends to be treated as
    total extraction failure, which is worse than a garbage-flagged
    pass-through). Stripping unconditionally after that point could
    silently hollow such text out to "" and defeat that fallback — so
    this wrapper backs off to the original text whenever stripping would
    otherwise leave nothing behind.
    """
    if not text or not text.strip():
        return text
    cleaned = strip_leaked_pdf_objects(text)
    return cleaned if cleaned.strip() else text


def ocr_pdf_bytes(content: bytes) -> str:
    """OCR a PDF (from raw bytes) by rendering each page as an image.

    Sidesteps whatever PDF text-layer parsing produced garbage, since OCR
    works purely off the rendered pixels.

    FALLBACK CHAIN:
      1. Local Tesseract (fast, free, no API key) — requires the
         `tesseract` system binary on PATH.
      2. Vision-LLM OCR via OpenRouter (google/gemini-2.5-flash, then
         openai/gpt-4o-mini) — needs only OPENROUTER_API_KEY in env.

    Most dev/prod environments have either Tesseract OR an OpenRouter key,
    so this chain covers both shapes of "OCR isn't working".
    """
    # --- Path 1: local Tesseract ---
    if _tesseract_available():
        try:
            import fitz  # PyMuPDF
            import pytesseract
            from PIL import Image

            doc = fitz.open(stream=content, filetype="pdf")
            text = ""
            for page in doc:
                pix = page.get_pixmap(dpi=200)
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                text += pytesseract.image_to_string(img) + "\n"
            doc.close()
            if text.strip():
                return text
            print("ℹ️  Tesseract returned no text from PDF — trying vision OCR fallback")
        except ImportError:
            print("ℹ️  PyMuPDF/pytesseract/Pillow not installed — using vision OCR fallback for PDF")
        except Exception as e:
            print(f"⚠️  Local Tesseract OCR failed ({e}) — trying vision OCR fallback")
    else:
        print("ℹ️  Local Tesseract unavailable — using vision OCR fallback for PDF")

    # --- Path 2: vision-LLM fallback ---
    return vision_ocr_pdf_bytes(content)


def ocr_pdf_path(file_path: str) -> str:
    """OCR a PDF (from a file path) by rendering each page as an image."""
    try:
        with open(file_path, "rb") as f:
            content = f.read()
    except Exception as e:
        print(f"⚠️  Could not read {file_path} for OCR fallback: {e}")
        return ""
    return ocr_pdf_bytes(content)


# ---------------------------------------------------------------------------
# Image OCR — same Tesseract-first / vision-LLM-fallback chain, for raw
# image bytes (PNG/JPEG/WebP/etc.). Used by file_processor._extract_from_image
# and any other code path that needs to OCR a single image.
# ---------------------------------------------------------------------------

def ocr_image_bytes(content: bytes, mime_type: str = "image/png") -> str:
    """OCR an image (from raw bytes).

    Same fallback chain as ocr_pdf_bytes(): Tesseract first (fast, free,
    no API key), then vision-LLM OCR via OpenRouter if Tesseract is
    unavailable or returns nothing usable.
    """
    # --- Path 1: local Tesseract ---
    if _tesseract_available():
        try:
            from io import BytesIO
            import pytesseract
            from PIL import Image

            img = Image.open(BytesIO(content))
            text = pytesseract.image_to_string(img)
            if text.strip():
                return text
            print("ℹ️  Tesseract returned no text from image — trying vision OCR fallback")
        except ImportError:
            print("ℹ️  pytesseract/Pillow not installed — using vision OCR fallback for image")
        except Exception as e:
            print(f"⚠️  Tesseract image OCR failed ({e}) — trying vision OCR fallback")
    else:
        print("ℹ️  Local Tesseract unavailable — using vision OCR fallback for image")

    # --- Path 2: vision-LLM fallback ---
    return vision_ocr_image_bytes(content, mime_type)


# ---------------------------------------------------------------------------
# Vision-LLM-based OCR (no system Tesseract binary needed)
# ---------------------------------------------------------------------------
#
# These functions are public so callers can use them directly when they
# specifically want to skip Tesseract (e.g. a caller that already tried
# Tesseract with custom preprocessing and got nothing). Most callers should
# use ocr_pdf_bytes() / ocr_image_bytes() instead — they auto-fallback.

def _tesseract_available() -> bool:
    """Return True iff the `tesseract` binary is installed and on PATH.

    Separated out as a helper so callers can decide explicitly whether to
    attempt the Tesseract path at all (rather than relying on the inner
    try/except, which treats ImportError and "binary missing" the same).
    """
    try:
        import shutil
        return shutil.which("tesseract") is not None
    except Exception:
        return False


def _get_vision_llm_client():
    """Lazily return a cached OpenAI client pointed at OpenRouter.

    Reuses m3_structurednotes.openai_client.get_client() to avoid creating
    a second OpenAI client (and a second connection pool) for the same
    OpenRouter account. Returns None if the client can't be initialised
    (missing API key, missing openai package, etc.) — callers handle that
    as an empty-result case.
    """
    try:
        from m3_structurednotes.openai_client import get_client
        return get_client()
    except Exception as e:
        print(f"⚠️  Could not init OpenRouter client for vision OCR: {e}")
        return None


# Vision-capable primary + fallback models on OpenRouter. Both are
# multimodal — they accept image_url input alongside text.
_VISION_OCR_PRIMARY_MODEL = "google/gemini-2.5-flash"
_VISION_OCR_FALLBACK_MODEL = "openai/gpt-4o-mini"


def vision_ocr_image_bytes(content: bytes, mime_type: str = "image/png") -> str:
    """Send image bytes to a vision-capable LLM via OpenRouter and return
    the transcribed text.

    Used as an automatic fallback when local Tesseract OCR is unavailable
    (no `tesseract` binary on PATH) or returns nothing usable. Significantly
    more accurate than Tesseract on small text, anti-aliased fonts, code
    snippets, formulas, and rotated/colored backgrounds.

    Returns "" on any failure (no API key, network error, model error, or
    the model reporting "NONE" for an image with no text) so callers can
    treat it the same as an empty Tesseract result.
    """
    import base64
    import os

    api_key = (os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        print("⚠️  Vision OCR unavailable: no OPENROUTER_API_KEY / OPENAI_API_KEY configured")
        return ""

    client = _get_vision_llm_client()
    if client is None:
        return ""

    # mime_type sanity: vision LLMs accept image/png, image/jpeg, image/webp,
    # image/gif. Default to png if caller passed something unusual.
    if not mime_type or not mime_type.startswith("image/"):
        mime_type = "image/png"

    b64_data = base64.b64encode(content).decode("utf-8")

    # Allow override via env (e.g. for testing or to pin a specific model).
    primary_model = (os.getenv("VISION_OCR_MODEL") or "").strip() or _VISION_OCR_PRIMARY_MODEL

    for model_name in (primary_model, _VISION_OCR_FALLBACK_MODEL):
        try:
            resp = client.chat.completions.create(
                model=model_name,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a high-accuracy OCR engine. Transcribe ALL visible text "
                            "in the image verbatim — including headings, body text, code, "
                            "formulas, table cells, captions, axis labels, footnotes, and small "
                            "print. Preserve line breaks and indentation where they occur. "
                            "Do NOT add commentary, descriptions, or markdown formatting. "
                            "Do NOT wrap the output in code fences. "
                            "If the image contains no text at all, reply with the single word: NONE"
                        ),
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{b64_data}"
                                },
                            }
                        ],
                    },
                ],
                temperature=0.0,
                max_tokens=4096,
            )
            text = (resp.choices[0].message.content or "").strip()
            if text.upper() == "NONE":
                return ""
            return text
        except Exception as e:
            print(f"⚠️  Vision OCR with {model_name} failed: {e}")
            continue
    return ""


def vision_ocr_pdf_bytes(content: bytes) -> str:
    """OCR a PDF by rendering each page as a PNG and sending each to the
    vision LLM. Used when local Tesseract is unavailable or returns nothing
    usable.

    Page-by-page (rather than whole-PDF-as-one-image) so we can stay under
    the per-request image size limit on OpenRouter, and so a single page
    failure doesn't lose the whole document.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        print("⚠️  PyMuPDF not installed — cannot render PDF pages for vision OCR")
        return ""

    try:
        doc = fitz.open(stream=content, filetype="pdf")
        page_texts = []
        for page_idx, page in enumerate(doc):
            try:
                pix = page.get_pixmap(dpi=200)
                png_bytes = pix.tobytes("png")
                page_text = vision_ocr_image_bytes(png_bytes, "image/png")
                if page_text:
                    page_texts.append(page_text)
                else:
                    print(f"ℹ️  Vision OCR returned no text for page {page_idx + 1}")
            except Exception as page_err:
                print(f"⚠️  Vision OCR failed for page {page_idx + 1}: {page_err}")
        doc.close()
        return "\n".join(page_texts)
    except Exception as e:
        print(f"⚠️  Vision OCR PDF rendering failed: {e}")
        return ""
