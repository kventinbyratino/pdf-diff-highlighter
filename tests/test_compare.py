from __future__ import annotations

import tempfile
from pathlib import Path
import base64
import io
import json

import fitz
import pytest
from PIL import Image

from app import app
from pdf_compare import (
    _compare_rendered_pages,
    _precision_to_threshold,
    compare_pdf_area,
    compare_pdfs,
    find_matching_area,
    RENDER_ZOOM,
)

COLORS = {
    'blue': (0, 0, 1),
    'red': (1, 0, 0),
    'green': (0, 0.6, 0),
}


@pytest.fixture(autouse=True)
def isolated_usage_metrics(tmp_path):
    old_path = app.config.get('USAGE_METRICS_PATH')
    metrics_path = tmp_path / 'usage_metrics.json'
    metrics_path.unlink(missing_ok=True)
    app.config['USAGE_METRICS_PATH'] = str(metrics_path)
    yield
    metrics_path.unlink(missing_ok=True)
    if old_path is None:
        app.config.pop('USAGE_METRICS_PATH', None)
    else:
        app.config['USAGE_METRICS_PATH'] = old_path


def count_red_pixels(image: Image.Image) -> int:
    rgb = image.convert('RGB')
    red_pixels = 0
    pixels = rgb.load()
    assert pixels is not None
    for x in range(rgb.width):
        for y in range(rgb.height):
            pixel = pixels[x, y]
            if not isinstance(pixel, tuple):
                continue
            r, g, b = pixel[:3]
            if r > 150 and g < 120 and b < 120:
                red_pixels += 1
    return red_pixels


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
        diff_image = Image.open(io.BytesIO(base64.b64decode(first.diff_image_b64))).convert('RGB')
        assert count_red_pixels(diff_image) > 0
        assert {row.kind for row in first.text_rows} <= {'Удалено', 'Добавлено'}

        second = result['pages'][1]
        assert second.text_changed is False
        assert second.text_rows == []


def test_sparse_faint_shifted_line_is_marked_red():
    left = Image.new('RGB', (1000, 1000), 'white')
    right = Image.new('RGB', (1000, 1000), 'white')
    for y in range(250, 750):
        left.putpixel((500, y), (238, 238, 238))
        right.putpixel((515, y), (238, 238, 238))

    diff_image, note, image_changed = _compare_rendered_pages(left, right, precision=50)

    assert image_changed is True
    assert 'визуальные изменения' in note
    assert count_red_pixels(diff_image) > 0


def test_precision_maps_to_thresholds():
    assert _precision_to_threshold(1) > _precision_to_threshold(100)
    assert _precision_to_threshold(1) >= 1
    assert _precision_to_threshold(100) == 1


def test_home_page_contains_pdf_only_controls():
    client = app.test_client()
    response = client.get('/')
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'Воспользовались сервисом' in html
    assert 'Сравнили чертежей' in html
    assert 'data-usage-users' in html
    assert 'data-usage-comparisons' in html
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
    assert 'Скачать сравнение' in html
    assert 'Полноэкранный просмотр' in html
    assert 'Скачать PNG' not in html
    assert 'pages-sidebar' not in html
    assert 'data-usage-comparisons>1</strong>' in html


def test_usage_metrics_count_unique_clients_and_successful_comparisons():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        left = tmp_path / 'left.pdf'
        right = tmp_path / 'right.pdf'
        make_pdf(left, [('Hello A', 'blue')])
        make_pdf(right, [('Hello B', 'red')])

        first_client = app.test_client()
        second_client = app.test_client()
        first_home = first_client.get('/').get_data(as_text=True)
        second_home = second_client.get('/').get_data(as_text=True)

        assert 'data-usage-users>' in first_home
        assert 'data-usage-users>' in second_home
        state = json.loads(Path(app.config['USAGE_METRICS_PATH']).read_text(encoding='utf-8'))
        assert len(state['visitors']) >= 2

        with left.open('rb') as left_file, right.open('rb') as right_file:
            response = first_client.post(
                '/compare',
                data={'pdf1': (left_file, 'left.pdf'), 'pdf2': (right_file, 'right.pdf')},
                content_type='multipart/form-data',
            )

    html = response.get_data(as_text=True)
    state = json.loads(Path(app.config['USAGE_METRICS_PATH']).read_text(encoding='utf-8'))
    assert response.status_code == 200
    assert len(state['visitors']) >= 2
    assert state['comparisons'] == 1
    assert 'data-usage-comparisons>1</strong>' in html


def test_compare_overlay_shows_mask_on_right_side():
    css = Path('static/style.css').read_text()
    assert 'clip-path: inset(0 0 0 var(--split, 50%));' in css


def make_area_pdf(path: Path, offset_x: float = 0, offset_y: float = 0, changed: bool = False) -> None:
    doc = fitz.open()
    page = doc.new_page(width=420, height=300)
    ox, oy = offset_x, offset_y
    page.draw_rect(fitz.Rect(90 + ox, 70 + oy, 230 + ox, 160 + oy), color=(0, 0, 0), width=1.5)
    page.draw_line((90 + ox, 115 + oy), (230 + ox, 115 + oy), color=(0, 0, 0), width=1.2)
    page.draw_line((160 + ox, 70 + oy), (160 + ox, 160 + oy), color=(0, 0, 0), width=1.2)
    if changed:
        page.draw_line((120 + ox, 90 + oy), (210 + ox, 145 + oy), color=(0, 0, 0), width=1.2)
    doc.save(path)
    doc.close()


def test_find_matching_area_detects_shifted_region():
    scale = RENDER_ZOOM / 1.5
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        left = tmp_path / 'left.pdf'
        right = tmp_path / 'right.pdf'
        make_area_pdf(left)
        make_area_pdf(right, offset_x=35, offset_y=25, changed=True)

        result = find_matching_area(
            str(left),
            str(right),
            {'x': int(round(120 * scale)), 'y': int(round(90 * scale)), 'width': int(round(260 * scale)), 'height': int(round(170 * scale))},
        )

    assert result['status'] in {'ok', 'low_confidence'}
    assert result['confidence'] > 0.65
    assert abs(result['targetRect']['x'] - round(172 * scale)) <= 35
    assert abs(result['targetRect']['y'] - round(128 * scale)) <= 35


def test_compare_pdf_area_returns_single_area_result():
    scale = RENDER_ZOOM / 1.5
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        left = tmp_path / 'left.pdf'
        right = tmp_path / 'right.pdf'
        make_area_pdf(left)
        make_area_pdf(right, offset_x=35, offset_y=25, changed=True)

        result = compare_pdf_area(
            str(left),
            str(right),
            source_rect={'x': int(round(120 * scale)), 'y': int(round(90 * scale)), 'width': int(round(260 * scale)), 'height': int(round(170 * scale))},
            target_rect={'x': int(round(172 * scale)), 'y': int(round(128 * scale)), 'width': int(round(260 * scale)), 'height': int(round(170 * scale))},
            precision=50,
        )

    assert result['area_mode'] is True
    assert len(result['pages']) == 1
    assert result['pages'][0].left_image_b64
    assert result['pages'][0].diff_image_b64


def test_area_button_and_modal_exist():
    client = app.test_client()
    response = client.get('/')
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'Сравнить область' in html
    assert 'data-area-mode' in html
    assert 'data-area-canvas="left"' in html
    assert 'data-area-canvas="right"' in html
