from __future__ import annotations

from pathlib import Path

import fitz
import pytest

from app import app
from pdf_compare import compare_pdfs
from quality.phase6_dataset import CASES, PAGE_SIZE, _show_transformed, generate_dataset


def discard_artifact(_page_number, _kind, _image):
    return 'test-only'


@pytest.fixture
def dataset(tmp_path):
    root = tmp_path / 'phase6'
    generate_dataset(root)
    return root


def test_optional_alignment_reaches_phase6_quality_targets(dataset):
    for case in CASES:
        result = compare_pdfs(
            str(dataset / case.left),
            str(dataset / case.right),
            precision=50,
            align_pages=True,
            artifact_writer=discard_artifact,
        )
        page = result['pages'][0]
        assert page.image_changed is case.expected_image_changed, case.name
        if case.expected_text_changed is not None:
            assert page.text_changed is case.expected_text_changed, case.name


def test_alignment_is_off_unless_explicitly_requested(dataset):
    page = compare_pdfs(
        str(dataset / 'vector_base.pdf'),
        str(dataset / 'vector_shift_2pt.pdf'),
        precision=50,
        align_pages=False,
        artifact_writer=discard_artifact,
    )['pages'][0]

    assert page.alignment_applied is False
    assert page.image_changed is True


def test_large_transform_is_rejected_by_safety_bounds(dataset):
    large_shift = dataset / 'vector_shift_60pt.pdf'
    _show_transformed(
        dataset / 'vector_base.pdf',
        large_shift,
        target=fitz.Rect(60, 60, PAGE_SIZE.width + 60, PAGE_SIZE.height + 60),
    )

    page = compare_pdfs(
        str(dataset / 'vector_base.pdf'),
        str(large_shift),
        precision=50,
        align_pages=True,
        artifact_writer=discard_artifact,
    )['pages'][0]

    assert page.alignment_applied is False
    assert page.image_changed is True
    assert page.alignment_summary.startswith('выравнивание не применено:')


def test_alignment_preserves_real_line_added_to_shifted_page(dataset):
    shifted_change = dataset / 'vector_added_line_shift_2pt.pdf'
    _show_transformed(
        dataset / 'vector_added_line.pdf',
        shifted_change,
        target=fitz.Rect(2, 2, PAGE_SIZE.width + 2, PAGE_SIZE.height + 2),
    )

    result = compare_pdfs(
        str(dataset / 'vector_base.pdf'),
        str(shifted_change),
        precision=50,
        align_pages=True,
        artifact_writer=discard_artifact,
    )
    page = result['pages'][0]

    assert page.alignment_applied is True
    assert page.image_changed is True
    assert 'структурные изменения' in page.note


def test_close_page_proportions_are_normalized(dataset):
    larger_size = fitz.Rect(0, 0, PAGE_SIZE.width * 1.005, PAGE_SIZE.height * 1.005)
    larger = dataset / 'vector_larger_same_ratio.pdf'
    _show_transformed(dataset / 'vector_base.pdf', larger, page_size=larger_size)

    page = compare_pdfs(
        str(dataset / 'vector_base.pdf'),
        str(larger),
        precision=50,
        align_pages=False,
        artifact_writer=discard_artifact,
    )['pages'][0]

    assert page.normalized_page_size is True
    assert page.incompatible_page_size is False
    assert page.image_changed is False
    assert page.image_width == page.diff_image_width
    assert page.image_height == page.diff_image_height
    assert 'размер страницы нормализован' in page.note


def test_incompatible_page_size_has_no_fake_diff_mask(dataset):
    page = compare_pdfs(
        str(dataset / 'vector_base.pdf'),
        str(dataset / 'vector_wider_page.pdf'),
        precision=50,
        align_pages=True,
        artifact_writer=discard_artifact,
    )['pages'][0]

    assert page.incompatible_page_size is True
    assert page.normalized_page_size is False
    assert page.image_changed is True
    assert page.diff_image_width != page.image_width
    assert 'маска изменений не строилась' in page.note


def test_pdf015_template_policy_for_incompatible_sizes_is_not_normal_diff_view():
    html = Path('templates/index.html').read_text(encoding='utf-8')

    assert '{% if p.incompatible_page_size and p.left_image_url and p.diff_image_url %}' in html
    assert '<strong>Размеры страниц несовместимы.</strong> Маска изменений не строилась.' in html
    incompatible_block = html.split('{% if p.incompatible_page_size', 1)[1].split('{% elif p.left_image_url', 1)[0]
    assert 'Исходный лист' in incompatible_block
    assert 'Изменённый лист' in incompatible_block
    assert 'Скачать сравнение' not in incompatible_block
    assert 'data-compare-slider' not in incompatible_block


def test_text_diff_is_skipped_when_only_one_pdf_has_extractable_text(dataset):
    page = compare_pdfs(
        str(dataset / 'vector_base.pdf'),
        str(dataset / 'scan_base.pdf'),
        precision=50,
        align_pages=True,
        artifact_writer=discard_artifact,
    )['pages'][0]

    assert page.text_comparison_skipped is True
    assert page.text_changed is False
    assert page.text_rows == []
    assert page.image_changed is False


def test_alignment_control_is_off_by_default_and_preserved_after_submit(tmp_path):
    old_metrics = app.config.get('USAGE_METRICS_PATH')
    old_artifacts = app.config.get('RESULT_ARTIFACT_ROOT')
    app.config['USAGE_METRICS_PATH'] = str(tmp_path / 'metrics.json')
    app.config['RESULT_ARTIFACT_ROOT'] = str(tmp_path / 'results')
    root = tmp_path / 'fixtures'
    generate_dataset(root)
    try:
        client = app.test_client()
        home = client.get('/').get_data(as_text=True)
        assert 'name="align_pages" type="checkbox" checked' not in home
        assert 'Выровнять листы перед сравнением' in home

        with (root / 'vector_base.pdf').open('rb') as left, (root / 'vector_shift_2pt.pdf').open('rb') as right:
            response = client.post(
                '/compare',
                data={
                    'pdf1': (left, 'left.pdf'),
                    'pdf2': (right, 'right.pdf'),
                    'precision': '50',
                    'align_pages': 'on',
                },
                content_type='multipart/form-data',
            )
        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert 'name="align_pages" type="checkbox" checked' in html
        assert 'выравнивание применено' in html
    finally:
        if old_metrics is None:
            app.config.pop('USAGE_METRICS_PATH', None)
        else:
            app.config['USAGE_METRICS_PATH'] = old_metrics
        if old_artifacts is None:
            app.config.pop('RESULT_ARTIFACT_ROOT', None)
        else:
            app.config['RESULT_ARTIFACT_ROOT'] = old_artifacts
