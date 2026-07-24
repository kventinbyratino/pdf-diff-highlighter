from __future__ import annotations

import json
from pathlib import Path

import fitz

from pdf_compare import compare_pdfs
from quality.phase6_dataset import CASES, generate_dataset

EXPECTED_CASES = {
    'vector_identical',
    'added_line',
    'removed_line',
    'text_change',
    'page_shift_2pt',
    'page_scale_1pct',
    'page_rotation_0_5deg',
    'different_page_size',
    'scan_identical',
    'vector_vs_scan',
}


def discard_artifact(_page_number, _kind, _image):
    return 'test-only'


def test_phase6_dataset_is_complete_and_valid(tmp_path):
    manifest = generate_dataset(tmp_path / 'fixtures')

    assert {case['name'] for case in manifest} == EXPECTED_CASES
    assert {case.name for case in CASES} == EXPECTED_CASES
    for case in CASES:
        for filename in (case.left, case.right):
            with fitz.open(tmp_path / 'fixtures' / filename) as document:
                assert document.page_count == 1


def test_phase6_stable_detection_invariants(tmp_path):
    root = tmp_path / 'fixtures'
    generate_dataset(root)
    expected = {
        'vector_identical': (False, False),
        'added_line': (True, False),
        'removed_line': (True, False),
        'text_change': (True, True),
        'different_page_size': (True, False),
        'scan_identical': (False, False),
    }

    by_name = {case.name: case for case in CASES}
    for name, outcome in expected.items():
        case = by_name[name]
        result = compare_pdfs(
            str(root / case.left),
            str(root / case.right),
            precision=50,
            artifact_writer=discard_artifact,
        )
        page = result['pages'][0]
        assert (page.image_changed, page.text_changed) == outcome


def test_checked_in_phase6_baseline_records_known_gaps():
    payload = json.loads(Path('quality/results/phase6-baseline.json').read_text(encoding='utf-8'))

    assert payload['schema_version'] == 1
    assert payload['summary']['case_count'] == 10
    assert payload['summary']['run_count'] == 30
    mismatches = {
        case['name']
        for case in payload['cases']
        if any(not run['target_match'] for run in case['runs'])
    }
    assert mismatches == {
        'page_shift_2pt',
        'page_scale_1pct',
        'page_rotation_0_5deg',
        'vector_vs_scan',
    }


def test_checked_in_phase6_aligned_candidate_closes_baseline_gaps():
    payload = json.loads(Path('quality/results/phase6-aligned.json').read_text(encoding='utf-8'))

    assert payload['align_pages'] is True
    assert payload['summary'] == {
        'case_count': 10,
        'run_count': 30,
        'target_matches': 30,
        'target_mismatches': 0,
    }
    assert all(run['target_match'] for case in payload['cases'] for run in case['runs'])
