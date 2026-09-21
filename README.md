# PDF Text Editor

A local web app for editing text directly inside a PDF, in place, while trying
to preserve the original font, size, color, and layout.

## How it works

- **Backend** (`backend/main.py`, FastAPI + [PyMuPDF](https://pymupdf.readthedocs.io/)):
  - `/upload` renders each PDF page as an image and extracts every text *span*
    (a run of text sharing one font/size/color) with its exact bounding box.
  - `/edit` takes your edited spans, **redacts** (erases) the original glyphs
    at their exact position, then **re-inserts** the new text at the same
    spot using a matching font size and color.
  - `/download` returns the modified PDF.
- **Frontend** (`frontend/index.html`): plain HTML/JS. Shows the rendered
  page image with an invisible, precisely-positioned, `contenteditable` box
  over every text span. Click any text to edit it right where it sits.

## Running it

Use Python 3.12 for the backend (Python 3.13 currently fails with the
PyMuPDF build on Windows):

```bash
cd backend
py -3.12 -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Then open `http://127.0.0.1:8000` in your browser. The backend serves the
frontend and API from the same origin.

To share the app temporarily, keep the backend running and use a tunnel in a
second terminal:

```bash
npx --yes localtunnel --port 8000
```

Share the HTTPS URL printed by the tunnel. The URL only works while both the
backend and tunnel terminals remain running.

## Publish a permanent public URL

This repository includes `render.yaml` for deployment on Render:

1. Push the project to a GitHub repository.
2. Sign in at [render.com](https://render.com) and choose **New > Blueprint**.
3. Connect the GitHub repository and select `render.yaml`.
4. Create the web service and wait for the build to finish.

Render will provide an HTTPS URL such as `https://pdf-text-editor.onrender.com`
that anyone can open. The free service may sleep after inactivity and wake on
the next visit. Uploaded PDFs are temporary and should not be treated as
permanent storage.

Open a PDF, click on any text to edit it, then click **Save edited PDF** to
download the result.

## What's preserved well

- Font size, text color, and exact x/y position of each edited text run.
- Bold/italic styling (mapped to the closest standard font).
- Everything you don't touch — images, vector graphics, untouched text — is
  left byte-for-byte as close to the original as PyMuPDF's re-save allows.

## Known limitations (honest list)

PDFs don't store "paragraphs" — each span is an independent, precisely
positioned run of glyphs in a specific embedded/non-embedded font. True
pixel-identical editing is a hard problem; this app handles the common case
well but has real edges:

1. **Font matching**: if the original font isn't embedded (or is an embedded
   subset we can't safely reuse), the app substitutes the closest standard
   font (Helvetica/Times/Courier, bold/italic). Distinctive custom or brand
   fonts won't be reproduced exactly.
2. **Background under erased text**: the app samples a pixel near the text to
   guess the fill color for erasing the old glyphs. This works well for solid
   backgrounds (white pages, flat colored boxes) but can leave a visible patch
   if the text sits on top of a photo, gradient, or fine pattern.
3. **Text length changes**: PDFs have no reflow model. If you make a span
   noticeably longer than the original, it will visually overflow into
   neighboring content rather than wrapping or shrinking. Same-length or
   modestly different edits work best.
4. **Rotated / curved text and complex scripts** (e.g. vertical CJK layouts,
   heavy kerning/ligatures) are extracted and can be edited, but positioning
   and shaping accuracy for these is lower than for plain horizontal Latin text.
5. Scanned/image-only PDFs have no extractable text layer — you'd need OCR
   first (not included here).

## Extending it

- Swap the fallback-font logic in `pick_fallback_font()` for real embedded-font
  reuse (PyMuPDF can extract embedded font file bytes via `page.get_fonts()`
  and `doc.extract_font()`, then you can register that exact font with
  `insert_font()` before calling `insert_text()`).
- Add auto-shrinking font size when new text would overflow the original box.
- Package as a desktop app with [pywebview](https://pywebview.flowrl.com/) or
  [Electron](https://www.electronjs.org/) wrapping this same frontend/backend.
