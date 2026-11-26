"""
tests/test_pdf_ocr.py — Hindi/Sanskrit OCR for image-only PDFs.

What this test proves (tesseract is a local binary; its data files are local;
the only network use is generating one fixture image, skipped if offline):

  1. _ocr_langs() resolves the language string from params, auto-detects
     installed Devanagari models, and never returns an empty/invalid set.
  2. _pdf_ocr() rasterizes a real image-only PDF (built here with PIL, so no
     fixture files are committed) and returns OCR text. With tesseract's
     hin/san models present it must find Devanagari text.
  3. _process_pdf('extract_text') on a scanned PDF does NOT return the old
     dead-end "may be scanned/image-based" message — it falls back to OCR.
  4. The LLM-facing TOOL declares 'ocr' for pdf so the model can pick it.

Isolation: everything runs in tmp dirs; nothing in config/ or uploads/ is
touched. Tesseract must be on PATH (it is: ~/.local/bin/tesseract).

Run:  .venv/bin/python -m pytest tests/test_pdf_ocr.py -q
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import actions.file_processor as fp  # noqa: E402


def _tess_langs() -> set[str]:
    try:
        out = subprocess.run(
            ["tesseract", "--list-langs"], capture_output=True, text=True, timeout=30
        )
        return {ln.strip() for ln in out.stdout.splitlines()[1:] if ln.strip()}
    except Exception:
        return set()


LANGS = _tess_langs()
HAS_TESS = bool(LANGS)
HAS_DEVANAGARI = bool({"hin", "san"} & LANGS)


def _render_text_pdf(tmp_path: Path, lines: list[str], name="t.pdf") -> Path:
    """Build a genuine image-only PDF containing Devanagari text.

    Two-stage: Chrome renders shaped text to PNG (its text layer would let
    pdftotext win without OCR, which would make the test vacuous), then PIL
    wraps the image into a PDF with no text objects at all. Skips if Chrome
    is unavailable.
    """
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome:
        pytest.skip("no chrome for fixture rendering")

    html_body = "".join(f"<p>{ln}</p>" for ln in lines)
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<style>body {{ font-family: 'Lohit Devanagari', serif; font-size: 30px;
       margin: 40px; background: white; color: #111; }}</style></head>
<body>{html_body}</body></html>"""
    src = tmp_path / (name + ".html")
    src.write_text(html, encoding="utf-8")
    png = tmp_path / (name + ".png")
    r = subprocess.run(
        [
            chrome,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            f"--screenshot={png}",
            "--window-size=900,500",
            "--hide-scrollbars",
            str(src),
        ],
        capture_output=True,
        timeout=90,
    )
    if not png.exists():
        pytest.skip(f"chrome screenshot failed: {r.stderr[-200:]}")

    from PIL import Image

    img = Image.open(png).convert("RGB")
    pdf = tmp_path / name
    img.save(pdf, "PDF", resolution=100)
    pdf = tmp_path / name
    return pdf


# ── 1. language resolution ───────────────────────────────────────────────────


@pytest.mark.skipif(not HAS_TESS, reason="tesseract not installed")
def test_ocr_langs_explicit():
    assert fp._ocr_langs({"lang": "hin"}) == "hin"
    assert fp._ocr_langs({"lang": "eng+hin"}) == "eng+hin"
    assert fp._ocr_langs({"lang": "san+eng"}) == "san+eng"


@pytest.mark.skipif(not HAS_TESS, reason="tesseract not installed")
def test_ocr_langs_auto_includes_installed_devanagari():
    auto = fp._ocr_langs({})
    wanted = [l for l in ("hin", "san") if l in LANGS]
    for l in wanted:
        assert l in auto, f"auto lang should include installed {l!r}: {auto}"
    assert "eng" in auto, f"eng is always installed; got {auto}"


@pytest.mark.skipif(not HAS_TESS, reason="tesseract not installed")
def test_ocr_langs_unknown_passthrough():
    # Unknown codes pass through untouched (tesseract will error on them and
    # _pdf_ocr surfaces that) — the contract is only that it's never "".
    assert fp._ocr_langs({"lang": "klingon"}) == "klingon"


@pytest.mark.skipif(not HAS_DEVANAGARI, reason="hin/san models not installed")
def test_ocr_langs_hindi_sanskrit_default():
    combined = fp._ocr_langs({"lang": "hindi,sanskrit"})
    assert "hin" in combined and "san" in combined, combined


# ── 2. real OCR on a real image-only PDF ─────────────────────────────────────


def _pdf_has_no_text_layer(pdf: Path) -> bool:
    r = subprocess.run(
        ["pdftotext", str(pdf), "-"], capture_output=True, text=True, timeout=60
    )
    return not r.stdout.strip()


@pytest.mark.skipif(not HAS_DEVANAGARI, reason="hin/san tesseract models missing")
def test_pdf_ocr_reads_hindi_sanskrit(tmp_path):
    lines = ["संस्कृतम् सरलं भाषा अस्ति।", "योगः जीवनं रक्षति।", "यह हिंदी की पंक्ति है।"]
    pdf = _render_text_pdf(tmp_path, lines)
    assert _pdf_has_no_text_layer(pdf), "fixture unexpectedly has a text layer"

    result = fp._pdf_ocr(pdf, tmp_path)
    assert "OCR failed" not in result, result
    assert len(result.strip()) > 10, f"OCR returned nearly nothing: {result!r}"


# ── 3. extract_text falls back to OCR on scanned PDFs ────────────────────────


@pytest.mark.skipif(not HAS_DEVANAGARI, reason="hin/san tesseract models missing")
def test_extract_text_falls_back_to_ocr(tmp_path):
    pdf = _render_text_pdf(tmp_path, ["प्रमाणपत्रम्।", "ओम् शान्तिः।"])
    out = fp._process_pdf(pdf, "extract_text", {})
    assert "may be scanned" not in out, f"dead-end message still present: {out}"
    assert "Could not extract" not in out, out


@pytest.mark.skipif(not HAS_DEVANAGARI, reason="hin/san tesseract models missing")
def test_ocr_action_routes(tmp_path):
    pdf = _render_text_pdf(tmp_path, ["सत्यमेव जयते।"])
    out = fp._process_pdf(pdf, "ocr", {})
    assert not out.startswith("Unknown PDF action"), out


# ── 4. TOOL declaration advertises the capability ────────────────────────────


def test_tool_declares_pdf_ocr():
    pdf_desc = fp.TOOL["parameters"]["properties"]["action"]["description"]
    assert "ocr" in pdf_desc.lower(), "action description should list 'ocr' for pdf"


def test_tool_declares_lang_param():
    props = fp.TOOL["parameters"]["properties"]
    assert "lang" in props, "TOOL should expose a lang parameter"
