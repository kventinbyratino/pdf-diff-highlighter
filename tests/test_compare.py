from __future__ import annotations

import tempfile
from pathlib import Path

import fitz
from PIL import Image

from pdf_compare import _precision_to_threshold, compare_images, compare_pdfs

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


def make_image(path: Path, color: tuple[int, int, int], size: tuple[int, int] = (180, 120)) -> None:
    img = Image.new('RGB', size, color)
    img.save(path)


def test_compare_detects_text_and_visual_changes():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        a = tmp / 'a.pdf'
        b = tmp / 'b.pdf'
        make_pdf(a, [('Hello A', 'blue'), ('Page 2 same', 'green')])
        make_pdf(b, [('Hello B', 'red'), ('Page 2 same', 'green')])
        result = compare_pdfs(str(a), str(b), precision=50)
        assert result['left_pages'] == 2
        assert result['right_pages'] == 2
        assert result['changed_pages'] >= 1
        assert result['precision'] == 50
        assert result['diff_threshold'] == _precision_to_threshold(50)
        first = result['pages'][0]
        assert first.text_changed is True
        assert first.image_changed is True
        assert first.text_rows
        kinds = {row.kind for row in first.text_rows}
        assert kinds <= {'Удалено', 'Добавлено'}
        second = result['pages'][1]
        assert second.text_changed is False
        assert second.text_rows == []


def test_compare_images_detects_changes_and_precision():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        a = tmp / 'a.png'
        b = tmp / 'b.png'
        make_image(a, (255, 255, 255))
        make_image(b, (240, 240, 240))
        result = compare_images(str(a), str(b), precision=80)
        assert result.left_size == (180, 120)
        assert result.right_size == (180, 120)
        assert result.changed is True
        assert result.note
        assert result.precision == 80
        assert result.diff_threshold == _precision_to_threshold(80)


def test_precision_maps_to_thresholds():
    assert _precision_to_threshold(1) > _precision_to_threshold(100)
    assert _precision_to_threshold(1) >= 1
    assert _precision_to_threshold(100) == 1
