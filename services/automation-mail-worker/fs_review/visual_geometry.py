from __future__ import annotations

from pathlib import Path

import fitz


def render_page_grid(
    pdf_path: Path,
    page_number: int,
    output_path: Path,
    *,
    zoom: float = 2.5,
) -> None:
    """Render one PDF page with a subtle normalized-coordinate ruler grid."""

    source = fitz.open(pdf_path)
    overlay = fitz.open()
    try:
        source_page = source[page_number - 1]
        page = overlay.new_page(
            width=source_page.rect.width,
            height=source_page.rect.height,
        )
        page.show_pdf_page(page.rect, source, page_number - 1)
        grid_color = (0.72, 0.84, 0.96)
        label_color = (0.30, 0.48, 0.68)
        for step in range(1, 20):
            fraction = step / 20
            x = page.rect.width * fraction
            y = page.rect.height * fraction
            page.draw_line(
                fitz.Point(x, 0),
                fitz.Point(x, page.rect.height),
                color=grid_color,
                width=0.15,
                overlay=True,
            )
            page.draw_line(
                fitz.Point(0, y),
                fitz.Point(page.rect.width, y),
                color=grid_color,
                width=0.15,
                overlay=True,
            )
            page.insert_text(
                fitz.Point(x + 1.2, 7),
                f"{fraction:.2f}",
                fontsize=3.5,
                color=label_color,
                overlay=True,
            )
            page.insert_text(
                fitz.Point(1.2, y - 1.0),
                f"{fraction:.2f}",
                fontsize=3.5,
                color=label_color,
                overlay=True,
            )
        pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        pixmap.save(output_path)
    finally:
        overlay.close()
        source.close()
