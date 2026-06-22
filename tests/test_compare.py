from __future__ import annotations

import tempfile
from pathlib import Path

import fitz

from app import app
from pdf_compare import _precision_to_threshold, compare_pdfs

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
        tmp_path = Path(tmp)
        left = tmp_path / 'left.pdf'
        right = tmp_path / 'right.pdf'
        make_pdf(left, [('Hello A', 'blue'), ('Page 2 same', 'green')])
        make_pdf(right, [('Hello B', 'red'), ('Page 2 same', 'green')])

        result = compare_pdfs(str(left), str(right), precision=50)

        assert result['left_pages'] == 2
        assert result['right_pages'] == 2
        assert result['changed_pages'] >= 1
        assert result['precision'] == 50
        assert result['diff_threshold'] == _precision_to_threshold(50)

        first = result['pages'][0]
        assert first.text_changed is True
        assert first.image_changed is True
        assert first.text_rows
        assert first.left_image_b64
        assert first.diff_image_b64
        assert {row.kind for row in first.text_rows} <= {'Удалено', 'Добавлено'}

        second = result['pages'][1]
        assert second.text_changed is False
        assert second.text_rows == []


def test_precision_maps_to_thresholds():
    assert _precision_to_threshold(1) > _precision_to_threshold(100)
    assert _precision_to_threshold(1) >= 1
    assert _precision_to_threshold(100) == 1


def test_home_page_contains_pdf_only_controls():
    client = app.test_client()
    response = client.get('/')
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'name="pdf1"' in html
    assert 'name="pdf2"' in html
    assert 'name="precision"' in html
    assert 'Сбросить' in html
    assert 'compare-images' not in html
    assert 'capture-screen' not in html
    assert 'data-cache' not in html


def test_compare_route_renders_before_after_slider():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        left = tmp_path / 'left.pdf'
        right = tmp_path / 'right.pdf'
        make_pdf(left, [('Hello A', 'blue')])
        make_pdf(right, [('Hello B', 'red')])

        client = app.test_client()
        with left.open('rb') as left_file, right.open('rb') as right_file:
            response = client.post(
                '/compare',
                data={
                    'pdf1': (left_file, 'left.pdf'),
                    'pdf2': (right_file, 'right.pdf'),
                    'precision': '60',
                },
                content_type='multipart/form-data',
            )

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'data-compare-slider' in html
    assert 'data-compare-range' in html
    assert 'Исходный файл' in html
    assert 'Маска изменений' in html
    assert 'pages-sidebar' not in html
