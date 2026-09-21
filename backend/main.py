"""
PDF Text Editor - Backend

Approach:
  1. /upload   - render each page to an image (for display) and extract every
                 text "span" (a run of characters sharing one font/size/color)
                 with its exact bounding box, via PyMuPDF's structured text dict.
  2. /edit     - for each edited span: redact (erase) the original glyphs from
                 the page, then re-insert the new string at the same position
                 using the closest matching font, size, and color.
  3. /download - return the modified PDF.

Formatting-preservation notes / known limits (see README.md):
  - If the original font is embedded in the PDF, we try to reuse it directly.
    If that's not possible (e.g. subset font we can't safely reuse, or a
    non-embedded system font), we fall back to the closest standard font
    (Helvetica/Times/Courier, bold/italic variants) at the same size & color.
  - Erasing old text assumes a roughly solid background under that text
    (we sample the pixel color at the span's location and use it as the
    redaction fill). Text sitting on top of complex images/gradients may
    show a visible patch.
  - If new text is significantly longer than the original, it will overflow
    the original bounding box (we don't reflow surrounding content, since a
    real PDF has no paragraph model - each span is an independent glyph run).
"""

import base64
import io
import json
import os
import uuid
from pathlib import Path

import fitz  # PyMuPDF
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

app = FastAPI(title="PDF Text Editor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

TMP_DIR = "/tmp/pdf_text_editor"
os.makedirs(TMP_DIR, exist_ok=True)
FRONTEND_INDEX = Path(__file__).resolve().parent.parent / "frontend" / "index.html"

# session_id -> {"path": str, "spans": {span_id: {...}}, "output_path": str|None}
SESSIONS = {}

ZOOM = 2.0  # render pages at 2x for crisper display

STANDARD_FONTS = {
    (False, False): "helv",
    (True, False): "hebo",
    (False, True): "heit",
    (True, True): "hebi",
}


def color_int_to_rgb(c: int):
    return ((c >> 16) & 255) / 255.0, ((c >> 8) & 255) / 255.0, (c & 255) / 255.0


def pick_fallback_font(flags: int) -> str:
    bold = bool(flags & (1 << 4))  # PyMuPDF span flag bit for bold-ish
    italic = bool(flags & (1 << 1))  # italic bit
    return STANDARD_FONTS[(bold, italic)]


def sample_background_color(page: "fitz.Page", rect: "fitz.Rect"):
    """Sample a pixel just above the span's bbox to guess the background fill."""
    try:
        probe = fitz.Rect(rect.x0, max(0, rect.y0 - 1), rect.x0 + 1, rect.y0)
        pix = page.get_pixmap(clip=probe, matrix=fitz.Matrix(1, 1))
        if pix.width > 0 and pix.height > 0:
            px = pix.pixel(0, 0)
            return tuple(c / 255.0 for c in px[:3])
    except Exception:
        pass
    return (1, 1, 1)  # default: white


def register_original_font(doc: "fitz.Document", page: "fitz.Page", span: dict):
    """Reuse the source font when the PDF exposes it; otherwise use a close fallback."""
    source_name = span["font"].split("+")[-1].lower()
    for font in page.get_fonts(full=True):
        xref, _, _, basefont, _, _ = font[:6]
        if basefont.split("+")[-1].lower() != source_name:
            continue
        try:
            _, _, _, font_data = doc.extract_font(xref)
            if font_data:
                fontname = f"original_{xref}"
                page.insert_font(fontname=fontname, fontbuffer=font_data)
                return fontname
        except Exception:
            break
    return pick_fallback_font(span["flags"])


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a .pdf file")

    data = await file.read()
    session_id = str(uuid.uuid4())
    path = os.path.join(TMP_DIR, f"{session_id}.pdf")
    with open(path, "wb") as f:
        f.write(data)

    doc = fitz.open(path)
    pages_out = []
    span_registry = {}
    counter = 0

    for page_index in range(len(doc)):
        page = doc[page_index]
        pix = page.get_pixmap(matrix=fitz.Matrix(ZOOM, ZOOM))
        img_b64 = base64.b64encode(pix.tobytes("png")).decode("ascii")

        text_dict = page.get_text("dict")
        spans_out = []
        for block in text_dict["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    if not span["text"].strip():
                        continue
                    span_id = f"s{counter}"
                    counter += 1
                    span_registry[span_id] = {
                        "page": page_index,
                        "bbox": span["bbox"],
                        "origin": span.get("origin"),
                        "font": span["font"],
                        "size": span["size"],
                        "color": span["color"],
                        "flags": span["flags"],
                        "text": span["text"],
                    }
                    spans_out.append(
                        {
                            "id": span_id,
                            "bbox": span["bbox"],
                            "text": span["text"],
                        }
                    )

        pages_out.append(
            {
                "index": page_index,
                "width": page.rect.width,
                "height": page.rect.height,
                "image": img_b64,
                "spans": spans_out,
            }
        )

    doc.close()
    SESSIONS[session_id] = {"path": path, "spans": span_registry, "output_path": None}
    return {"session_id": session_id, "pages": pages_out}


@app.post("/edit")
async def edit(session_id: str = Form(...), edits: str = Form(...)):
    if session_id not in SESSIONS:
        raise HTTPException(404, "Unknown session")

    edits_map = json.loads(edits)  # {span_id: new_text}
    session = SESSIONS[session_id]
    doc = fitz.open(session["path"])

    by_page = {}
    for span_id, new_text in edits_map.items():
        if span_id not in session["spans"]:
            continue
        span = session["spans"][span_id]
        by_page.setdefault(span["page"], []).append((span, new_text))

    for page_index, items in by_page.items():
        page = doc[page_index]

        # Pass 1: figure out fill color per span, then redact (erase old glyphs)
        prepared = []
        for span, new_text in items:
            rect = fitz.Rect(span["bbox"])
            bg = sample_background_color(page, rect)
            page.add_redact_annot(rect, fill=bg)
            prepared.append((span, new_text))
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

        # Pass 2: insert replacement text
        for span, new_text in prepared:
            x0, y0, x1, y1 = span["bbox"]
            r, g, b = color_int_to_rgb(span["color"])
            fontname = register_original_font(doc, page, span)
            fontsize = span["size"]
            origin = span.get("origin")
            baseline = tuple(origin) if origin else (x0, y1 - (y1 - y0) * 0.2)

            page.insert_text(
                baseline,
                new_text,
                fontsize=fontsize,
                fontname=fontname,
                color=(r, g, b),
            )

    out_path = session["path"].replace(".pdf", "_edited.pdf")
    doc.save(out_path, garbage=4, deflate=True)
    doc.close()
    session["output_path"] = out_path
    return {"status": "ok"}


@app.get("/download/{session_id}")
async def download(session_id: str):
    session = SESSIONS.get(session_id)
    if not session or not session.get("output_path"):
        raise HTTPException(404, "No edited file yet for this session")
    return FileResponse(
        session["output_path"], filename="edited.pdf", media_type="application/pdf"
    )


@app.get("/")
async def root():
    return FileResponse(FRONTEND_INDEX, media_type="text/html")
