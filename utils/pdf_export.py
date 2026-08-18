"""
GitPulse - minimal dependency-free PDF writer.

Generates a valid, paginated PDF report using only the standard library.
This keeps the Team Reports export feature working everywhere without a
heavy PDF package (reportlab / weasyprint).

Implementation notes:
* A4 pages, Helvetica (regular + bold) from the PDF base-14 fonts.
* Text is sanitized to ASCII so the base-14 fonts always render it.
* Layout is intentionally simple: headings, wrapped paragraphs, simple
  line tables and page breaks.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

PAGE_W = 595.28
PAGE_H = 841.89
MARGIN = 48
CONTENT_W = PAGE_W - 2 * MARGIN

# Approximate Helvetica glyph width factor, used for word-wrapping.
_GLYPH_W = 0.5


def _sanitize(value: Any) -> str:
    """Convert to str and strip characters the base-14 fonts cannot show."""
    text = str(value if value is not None else "")
    out = []
    for ch in text:
        if ord(ch) < 128:
            out.append(ch)
        elif ch == "\u2190" or ch == "\u2192":  # arrows -> "-"
            out.append("-")
        else:
            out.append("?")
    return "".join(out)


def _esc(text: str) -> str:
    """Escape a string for inclusion in a PDF content stream."""
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _wrap(text: str, size: float, max_width: float) -> list[str]:
    """Greedy word-wrap to a pixel width."""
    words = _sanitize(text).split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) * size * _GLYPH_W <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            while len(word) * size * _GLYPH_W > max_width:
                cut = max(1, int(max_width / (size * _GLYPH_W)))
                lines.append(word[:cut])
                word = word[cut:]
            current = word
    if current:
        lines.append(current)
    return lines or [""]


class _PDF:
    """Accumulates content streams and knows the current cursor position."""

    def __init__(self) -> None:
        self.pages: list[list[str]] = [[]]
        self.y = MARGIN  # distance from the TOP of the current page

    def _ensure(self, height: float) -> None:
        if self.y + height > PAGE_H - MARGIN:
            self.pages.append([])
            self.y = MARGIN

    def _move(self, dy: float) -> None:
        self.y += dy

    def text(self, x: float, text: str, size: float, bold: bool = False) -> None:
        pdf_y = PAGE_H - (self.y + size)
        font = "/F2" if bold else "/F1"
        self.pages[-1].append(
            f"BT {font} {size:.1f} Tf 0.85 0.86 0.90 rg "
            f"1 0 0 1 {x:.1f} {pdf_y:.1f} Tm ({_esc(_sanitize(text))}) Tj ET"
        )

    def heading(self, text: str) -> None:
        self._ensure(34)
        self._move(14)
        pdf_y = PAGE_H - (self.y + 11)
        self.pages[-1].append(
            f"BT /F2 12 Tf 0.486 0.361 1.000 rg "
            f"1 0 0 1 {MARGIN:.1f} {pdf_y:.1f} Tm ({_esc(_sanitize(text))}) Tj ET"
        )
        self._move(18)

    def rule(self) -> None:
        self._ensure(6)
        y = PAGE_H - self.y
        self.pages[-1].append(
            f"0.18 0.20 0.26 RG {MARGIN:.1f} {y:.1f} m "
            f"{PAGE_W - MARGIN:.1f} {y:.1f} l S"
        )
        self._move(8)

    def para(self, text: str, size: float = 9.5) -> None:
        for chunk in _wrap(text, size, CONTENT_W):
            self._ensure(size * 1.6)
            self.text(MARGIN, chunk, size)
            self._move(size * 1.5)

    def kv(self, label: str, value: str, size: float = 9.5) -> None:
        self._ensure(size * 1.6)
        pdf_y = PAGE_H - (self.y + size)
        self.pages[-1].append(
            f"BT /F2 {size:.1f} Tf 0.85 0.86 0.90 rg "
            f"1 0 0 1 {MARGIN:.1f} {pdf_y:.1f} Tm "
            f"({_esc(_sanitize(label))}) Tj ET"
        )
        self.text(MARGIN + 165, value, size)
        self._move(size * 1.5)

    def table(
        self,
        headers: list[str],
        rows: Iterable[list[Any]],
        widths: list[float],
        row_height: float = 13.0,
    ) -> None:
        headers = [_sanitize(h) for h in headers]
        total = sum(widths) or 1
        xs: list[float] = []
        cursor = MARGIN
        for w in widths:
            xs.append(cursor)
            cursor += CONTENT_W * (w / total)

        def _row(cells: list[Any], bold: bool) -> None:
            self._ensure(row_height + 4)
            top = PAGE_H - self.y
            self.pages[-1].append(
                f"0.22 0.25 0.32 RG {MARGIN:.1f} {top:.1f} m "
                f"{PAGE_W - MARGIN:.1f} {top:.1f} l S"
            )
            y_base = PAGE_H - (self.y + 8.5)
            for i, cell in enumerate(cells):
                if i >= len(xs):
                    break
                self.pages[-1].append(
                    f"BT {'/F2' if bold else '/F1'} 8.5 Tf "
                    f"0.85 0.86 0.90 rg 1 0 0 1 {xs[i]:.1f} {y_base:.1f} Tm "
                    f"({_esc(_sanitize(cell))}) Tj ET"
                )
            self._move(row_height)

        self._ensure(20)
        _row(headers, True)
        for row in rows:
            _row(list(row), False)


def report_to_pdf(view: dict[str, Any]) -> bytes:
    """Render the Team Reports view into a PDF byte stream."""
    pdf = _PDF()
    repo = (view.get("repo") or {}).get("name", "")
    rng = view["range"]

    # -- Title block -----------------------------------------------------
    pdf.text(MARGIN, "GitPulse Team Report", 16, bold=True)
    pdf._move(20)
    pdf.text(MARGIN, f"Repository: {repo}", 10.5, bold=True)
    pdf._move(15)
    pdf.text(MARGIN, f"Period: {rng['label']}  ({rng['since']} to {rng['until']})", 9)
    pdf._move(13)
    pdf.text(
        MARGIN,
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        8.5,
    )
    pdf._move(12)
    pdf.rule()

    # -- Team summary ----------------------------------------------------
    pdf.heading("Team Summary")
    summary_labels = {
        "total_members": "Total Members",
        "active_members": "Active Members",
        "inactive_members": "Inactive Members",
        "total_commits": "Total Commits",
        "pull_requests": "Pull Requests",
        "merged_pull_requests": "Merged Pull Requests",
        "open_issues": "Open Issues",
        "code_reviews": "Code Reviews",
    }
    for key, value in (view["summary"] or {}).items():
        pdf.kv(summary_labels.get(key, key) + ":", str(value))

    # -- Member performance ----------------------------------------------
    pdf.heading("Member Performance")
    headers = ["Member", "Commits", "PRs", "Merged", "Reviews", "Issues", "Score", "Status"]
    widths = [26, 10, 9, 9, 9, 9, 9, 19]
    rows = [
        [
            r["username"], r["commits"], r["prs_created"], r["prs_merged"],
            r["prs_reviewed"], r["issues_created"], r["activity_score"],
            r["activity_status"],
        ]
        for r in view["members"]
    ]
    if not rows:
        rows = [["No members in the selected period.", "", "", "", "", "", "", ""]]
    pdf.table(headers, rows, widths)

    # -- Top contributors -------------------------------------------------
    pdf.heading("Top Contributors")
    pdf.table(
        ["Rank", "Username", "Commits", "Activity Score"],
        [
            [r["rank"], r["username"], r["commits"], r["activity_score"]]
            for r in view["top_contributors"]
        ],
        [15, 50, 20, 15],
    )

    # -- Insights --------------------------------------------------------
    pdf.heading("Team Insights")
    for insight in view["insights"]:
        pdf.kv(insight["title"] + ":", f"{insight['value']} - {insight['detail']}")

    # -- Team analysis ----------------------------------------------------
    pdf.heading("Team Analysis")
    ai = view.get("ai") or {}
    pdf.para(ai.get("summary") or "No analysis available.")

    body, offsets = _assemble(pdf.pages)
    header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    xref_pos = len(header) + len(body)
    total = len(offsets)
    xref = bytearray()
    xref += f"xref\n0 {total}\n".encode()
    xref += b"0000000000 65535 f \n"
    for off in offsets:
        xref += f"{off + len(header):010d} 00000 n \n".encode()
    xref += (
        f"trailer\n<< /Size {total} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    )
    return header + body + bytes(xref)


def _assemble(pages: list[list[str]]) -> bytes:
    """Build the final PDF body (objects), tracking xref offsets explicitly."""
    body = bytearray()
    offsets: list[int] = []

    def add_obj(payload: bytes) -> int:
        """Append 'N 0 obj' + payload, return object id."""
        obj_id = len(offsets) + 1
        offsets.append(len(body))
        body.extend(f"{obj_id} 0 obj\n".encode())
        body.extend(payload)
        body.extend(b"\nendobj\n")
        return obj_id

    catalog = b"<< /Type /Catalog /Pages 2 0 R >>"
    pages_obj = (
        b"<< /Type /Pages /Kids ["
        + b" ".join(f"{5 + i} 0 R".encode() for i in range(len(pages)))
        + b"] /Count %d >>" % len(pages)
    )
    f1 = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    f2 = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>"

    add_obj(catalog)
    add_obj(pages_obj)
    add_obj(f1)
    add_obj(f2)

    stream_index = 5 + len(pages)
    for i, ops in enumerate(pages):
        stream_id = stream_index + i
        page = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f] "
            % (PAGE_W, PAGE_H)
            + b"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents "
            + f"{stream_id} 0 R".encode()
            + b" >>"
        )
        add_obj(page)

    for ops in pages:
        content = b"\n".join(op.encode("latin-1") for op in ops)
        stream = b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"
        add_obj(stream)

    return bytes(body), offsets
