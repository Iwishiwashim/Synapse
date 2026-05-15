from __future__ import annotations

"""
Converts various file formats to plain text for AI extraction.
All readers return str or None (None = skip this file).
Only stdlib + optional soft deps — missing libs are caught gracefully.
"""

import csv
import html.parser
import io
import json
from pathlib import Path


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def read_pdf(path: Path, max_chars: int = 12_000) -> str | None:
    try:
        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(str(path))
        pages = []
        for page in reader.pages:
            text = page.extract_text() or ""
            pages.append(text)
            if sum(len(p) for p in pages) >= max_chars:
                break
        return "\n\n".join(pages)[:max_chars] or None
    except ImportError:
        pass

    try:
        import pdfplumber  # type: ignore
        with pdfplumber.open(str(path)) as pdf:
            pages = []
            for page in pdf.pages:
                text = page.extract_text() or ""
                pages.append(text)
                if sum(len(p) for p in pages) >= max_chars:
                    break
        return "\n\n".join(pages)[:max_chars] or None
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------

def read_docx(path: Path, max_chars: int = 12_000) -> str | None:
    try:
        from docx import Document  # type: ignore
        doc = Document(str(path))
        lines = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n".join(lines)[:max_chars] or None
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def read_csv(path: Path, max_rows: int = 50) -> str | None:
    try:
        with open(path, newline="", encoding="utf-8", errors="ignore") as f:
            reader = csv.reader(f)
            rows = []
            for i, row in enumerate(reader):
                if i > max_rows:
                    break
                rows.append(row)
        if not rows:
            return None
        header = rows[0]
        sample = rows[1:6]
        lines = [
            f"CSV file: {path.name}",
            f"Columns ({len(header)}): {', '.join(header)}",
            f"Total rows shown: {len(rows) - 1}",
            "",
            "Sample rows:",
        ]
        for row in sample:
            lines.append("  " + " | ".join(row))
        return "\n".join(lines)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Jupyter Notebook (.ipynb)
# ---------------------------------------------------------------------------

def read_ipynb(path: Path, max_chars: int = 12_000) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None

    cells = data.get("cells", [])
    parts: list[str] = [f"Jupyter Notebook: {path.name}", ""]

    for cell in cells:
        ctype = cell.get("cell_type", "")
        source = "".join(cell.get("source", []))
        if not source.strip():
            continue
        if ctype == "markdown":
            parts.append(source)
        elif ctype == "code":
            parts.append(f"```python\n{source}\n```")
            # include text outputs (skip images)
            for output in cell.get("outputs", []):
                if output.get("output_type") in ("stream", "execute_result"):
                    text = "".join(output.get("text", []) or output.get("data", {}).get("text/plain", []))
                    if text.strip():
                        parts.append(f"Output:\n{text[:500]}")
        parts.append("")

    return "\n".join(parts)[:max_chars] or None


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

class _HTMLStripper(html.parser.HTMLParser):
    _SKIP_TAGS = {"script", "style", "head", "meta", "link"}

    def __init__(self) -> None:
        super().__init__()
        self._skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag.lower() in self._SKIP_TAGS:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._SKIP_TAGS and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def read_html(path: Path, max_chars: int = 12_000) -> str | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
        stripper = _HTMLStripper()
        stripper.feed(raw)
        text = "\n".join(stripper.parts)
        return text[:max_chars] or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_READERS = {
    ".pdf":   read_pdf,
    ".docx":  read_docx,
    ".csv":   read_csv,
    ".ipynb": read_ipynb,
    ".html":  read_html,
    ".htm":   read_html,
}

SUPPORTED_EXTENSIONS: set[str] = set(_READERS)


def extract_text(path: Path, max_chars: int = 12_000) -> str | None:
    """Return plain text from any supported file type, or None if unsupported/unreadable."""
    reader = _READERS.get(path.suffix.lower())
    if reader is None:
        return None
    try:
        return reader(path, max_chars)  # type: ignore[call-arg]
    except Exception:
        return None
