from __future__ import annotations

import base64
import difflib
import io
from dataclasses import dataclass, field
from typing import Any

import fitz  # PyMuPDF
from PIL import Image, ImageChops, ImageStat


@dataclass
class TextChange:
    source: str = ""
    changed: str = ""
    kind: str = ""


@dataclass
class PageComparison:
    page_number: int
    text_changed: bool
    image_changed: bool
    missing_left: bool = False
    missing_right: bool = False
    text_rows: list[TextChange] = field(default_factory=list)
    left_image_b64: str = ""
    diff_image_b64: str = ""
    note: str = ""


def _img_to_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode('ascii')


def _render_page(page: fitz.Page, zoom: float = 1.5) -> Image.Image:
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return Image.frombytes('RGB', (pix.width, pix.height), pix.samples)


def _page_text(page: fitz.Page) -> str:
    return str(page.get_text('text')).strip()


def _line_rows(left: str, right: str) -> list[TextChange]:
    if left == right:
        return []

    left_lines = left.splitlines()
    right_lines = right.splitlines()
    sm = difflib.SequenceMatcher(a=left_lines, b=right_lines)
    rows: list[TextChange] = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            continue
        if tag in ('delete', 'replace'):
            rows.extend(TextChange(source=line, kind='Удалено') for line in left_lines[i1:i2])
        if tag in ('insert', 'replace'):
            rows.extend(TextChange(changed=line, kind='Добавлено') for line in right_lines[j1:j2])

    return rows


def _precision_to_threshold(precision: int) -> int:
    precision = max(1, min(100, int(precision)))
    return max(1, round(35 - (precision - 1) * 34 / 99))


def _compare_rendered_pages(left: Image.Image, right: Image.Image, precision: int) -> tuple[Image.Image, str, bool]:
    threshold = _precision_to_threshold(precision)

    if left.size != right.size:
        canvas = Image.new('RGB', (left.width + right.width + 20, max(left.height, right.height)), 'white')
        canvas.paste(left, (0, 0))
        canvas.paste(right, (left.width + 20, 0))
        return canvas, f'разный размер страниц: {left.size} vs {right.size}', True

    diff = ImageChops.difference(left, right)
    stat = ImageStat.Stat(diff)
    mean = sum(stat.mean) / len(stat.mean)

    if diff.getbbox() is None or mean < 1.0:
        return right.copy(), '', False

    mask = diff.convert('L').point(lambda p: 255 if int(p) > threshold else 0)  # type: ignore[assignment]
    if mask.getbbox() is None:
        return right.copy(), '', False

    highlight = Image.new('RGB', left.size, 'red')
    merged = Image.composite(highlight, right, mask)
    blended = Image.blend(right, merged, 0.62)
    return blended, f'визуальные изменения: mean diff={mean:.2f}, threshold={threshold}', True


def _missing_page(page_number: int, *, missing_left: bool, missing_right: bool) -> PageComparison:
    return PageComparison(
        page_number=page_number,
        text_changed=True,
        image_changed=True,
        missing_left=missing_left,
        missing_right=missing_right,
        text_rows=[TextChange(source='(страница отсутствует)', changed='(страница отсутствует)', kind='Страница отличается')],
        note='страница есть только в одном PDF',
    )


def compare_pdfs(left_path: str, right_path: str, precision: int = 50) -> dict[str, Any]:
    precision = max(1, min(100, int(precision)))
    pages: list[PageComparison] = []

    with fitz.open(left_path) as left_doc, fitz.open(right_path) as right_doc:
        left_pages = left_doc.page_count
        right_pages = right_doc.page_count
        max_pages = max(left_pages, right_pages)

        for idx in range(max_pages):
            left_exists = idx < left_pages
            right_exists = idx < right_pages
            page_number = idx + 1

            if not left_exists or not right_exists:
                pages.append(_missing_page(page_number, missing_left=not left_exists, missing_right=not right_exists))
                continue

            left_page = left_doc[idx]
            right_page = right_doc[idx]
            left_text = _page_text(left_page)
            right_text = _page_text(right_page)
            left_img = _render_page(left_page)
            right_img = _render_page(right_page)
            diff_img, note, image_changed = _compare_rendered_pages(left_img, right_img, precision)
            text_rows = _line_rows(left_text, right_text)

            pages.append(PageComparison(
                page_number=page_number,
                text_changed=bool(text_rows),
                image_changed=image_changed,
                text_rows=text_rows,
                left_image_b64=_img_to_b64(left_img),
                diff_image_b64=_img_to_b64(diff_img),
                note=note,
            ))

            left_img.close()
            right_img.close()
            diff_img.close()

    return {
        'left_pages': left_pages,
        'right_pages': right_pages,
        'pages': pages,
        'changed_pages': sum(1 for page in pages if page.text_changed or page.image_changed),
        'precision': precision,
        'diff_threshold': _precision_to_threshold(precision),
    }
