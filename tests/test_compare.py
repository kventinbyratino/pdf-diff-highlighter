from __future__ import annotations

import tempfile
from pathlib import Path

import fitz

from pdf_compare import compare_pdfs

COLORS = {
    'blue': (0, 0, 1),
    'red': (1, 0, 0),
    'green': (0, 0.6, 0),
}


def make_pdf(path: Path, pages: list[tuple[str, str]]) -> None:
    doc = fitz.open()
    for text, color_name in pages:
        color = COLORS[color_name]
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=18, color=color)
        page.draw_rect(fitz.Rect(72, 120, 220, 200), color=color, fill=color, width=2)
    doc.save(path)
    doc.close()


def test_compare_detects_text_and_visual_changes():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        a = tmp / 'a.pdf'
        b = tmp / 'b.pdf'
        make_pdf(a, [('Hello A', 'blue'), ('Page 2 same', 'green')])
        make_pdf(b, [('Hello B', 'red'), ('Page 2 same', 'green')])
        result = compare_pdfs(str(a), str(b))
        assert result['left_pages'] == 2
        assert result['right_pages'] == 2
        assert result['changed_pages'] >= 1
        first = result['pages'][0]
        assert first.text_changed is True
        assert first.image_changed is True
        assert first.text_rows
        kinds = {row.kind for row in first.text_rows}
        assert kinds <= {'Удалено', 'Добавлено'}
        second = result['pages'][1]
        assert second.text_changed is False
        assert second.text_rows == []
