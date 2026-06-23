from __future__ import annotations

import base64
import difflib
import io
from dataclasses import dataclass, field
from typing import Any

import fitz  # PyMuPDF
from PIL import Image, ImageChops, ImageFilter, ImageStat
import cv2
import numpy as np

RENDER_ZOOM = 3.0
DIFF_MASK_OPACITY = 0.8
DIFF_MASK_FEATHER_RADIUS = 1.2


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


def _render_page(page: fitz.Page, zoom: float = RENDER_ZOOM) -> Image.Image:
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return Image.frombytes('RGB', (pix.width, pix.height), pix.samples)


def _clamp_rect(rect: dict[str, Any], size: tuple[int, int]) -> tuple[int, int, int, int]:
    width, height = size
    x = int(round(float(rect.get('x', 0))))
    y = int(round(float(rect.get('y', 0))))
    w = int(round(float(rect.get('width', 0))))
    h = int(round(float(rect.get('height', 0))))
    x = max(0, min(x, max(0, width - 1)))
    y = max(0, min(y, max(0, height - 1)))
    w = max(1, min(w, width - x))
    h = max(1, min(h, height - y))
    return x, y, w, h


def _crop_rect(image: Image.Image, rect: dict[str, Any]) -> Image.Image:
    x, y, w, h = _clamp_rect(rect, image.size)
    return image.crop((x, y, x + w, y + h))


def render_pdf_page_preview(path: str, page_index: int = 0) -> dict[str, Any]:
    with fitz.open(path) as doc:
        if doc.page_count == 0:
            raise ValueError('PDF не содержит страниц')
        page_index = max(0, min(page_index, doc.page_count - 1))
        image = _render_page(doc[page_index])
        try:
            return {
                'page': page_index,
                'pages': doc.page_count,
                'width': image.width,
                'height': image.height,
                'image': _img_to_b64(image),
            }
        finally:
            image.close()


def _preprocess_for_matching(image: Image.Image) -> np.ndarray:
    gray = np.array(image.convert('L'))
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    _, binary = cv2.threshold(blurred, 245, 255, cv2.THRESH_BINARY_INV)
    edges = cv2.Canny(blurred, 50, 150)
    return cv2.max(binary, edges)


def find_matching_area(left_path: str, right_path: str, source_rect: dict[str, Any], page_index: int = 0) -> dict[str, Any]:
    with fitz.open(left_path) as left_doc, fitz.open(right_path) as right_doc:
        if page_index >= left_doc.page_count or page_index >= right_doc.page_count:
            raise ValueError('Выбранный лист отсутствует в одном из PDF')
        left_img = _render_page(left_doc[page_index])
        right_img = _render_page(right_doc[page_index])

    try:
        x, y, w, h = _clamp_rect(source_rect, left_img.size)
        if w < 40 or h < 40:
            raise ValueError('Область слишком мала для надёжного поиска')

        source_crop = left_img.crop((x, y, x + w, y + h))
        right_match = _preprocess_for_matching(right_img)
        template_original = _preprocess_for_matching(source_crop)

        best: dict[str, Any] = {'confidence': -1.0, 'targetRect': None, 'scale': 1.0}
        for scale in np.linspace(0.90, 1.10, 11):
            scaled_w = max(1, int(round(w * float(scale))))
            scaled_h = max(1, int(round(h * float(scale))))
            if scaled_w > right_match.shape[1] or scaled_h > right_match.shape[0]:
                continue
            template = cv2.resize(template_original, (scaled_w, scaled_h), interpolation=cv2.INTER_AREA)
            if int(np.count_nonzero(template)) < 20:
                continue
            result = cv2.matchTemplate(right_match, template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)
            if float(max_val) > float(best['confidence']):
                best = {
                    'confidence': float(max_val),
                    'targetRect': {
                        'x': int(max_loc[0]),
                        'y': int(max_loc[1]),
                        'width': int(scaled_w),
                        'height': int(scaled_h),
                    },
                    'scale': round(float(scale), 3),
                }

        source_crop.close()
        if not best['targetRect']:
            raise ValueError('Не удалось найти соответствующую область')

        confidence = float(best['confidence'])
        status = 'ok' if confidence >= 0.85 else 'low_confidence' if confidence >= 0.65 else 'manual_required'
        return {
            'status': status,
            'sourceRect': {'x': x, 'y': y, 'width': w, 'height': h},
            'targetRect': best['targetRect'],
            'scale': best['scale'],
            'confidence': round(confidence, 4),
            'message': '' if status == 'ok' else 'Проверьте найденную область вручную',
        }
    finally:
        left_img.close()
        right_img.close()


def compare_pdf_area(
    left_path: str,
    right_path: str,
    source_rect: dict[str, Any],
    target_rect: dict[str, Any],
    page_index: int = 0,
    precision: int = 50,
) -> dict[str, Any]:
    precision = max(1, min(100, int(precision)))
    with fitz.open(left_path) as left_doc, fitz.open(right_path) as right_doc:
        if page_index >= left_doc.page_count or page_index >= right_doc.page_count:
            raise ValueError('Выбранный лист отсутствует в одном из PDF')
        left_pages = left_doc.page_count
        right_pages = right_doc.page_count
        left_img = _render_page(left_doc[page_index])
        right_img = _render_page(right_doc[page_index])

    left_crop = _crop_rect(left_img, source_rect)
    right_crop = _crop_rect(right_img, target_rect)
    left_img.close()
    right_img.close()
    if right_crop.size != left_crop.size:
        resized = right_crop.resize(left_crop.size, Image.Resampling.BICUBIC)
        right_crop.close()
        right_crop = resized

    diff_img, note, image_changed = _compare_rendered_pages(left_crop, right_crop, precision)
    page = PageComparison(
        page_number=page_index + 1,
        text_changed=False,
        image_changed=image_changed,
        text_rows=[],
        left_image_b64=_img_to_b64(left_crop),
        diff_image_b64=_img_to_b64(diff_img),
        note=f'сравнение выбранной области; {note}'.strip('; '),
    )
    left_crop.close()
    right_crop.close()
    diff_img.close()
    return {
        'left_pages': left_pages,
        'right_pages': right_pages,
        'pages': [page],
        'changed_pages': 1 if image_changed else 0,
        'precision': precision,
        'diff_threshold': _precision_to_threshold(precision),
        'area_mode': True,
    }


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
    return max(1, round(12 - (precision - 1) * 11 / 99))


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

    if diff.getbbox() is None:
        return right.copy(), '', False

    mask = diff.convert('L').point(lambda p: 255 if int(p) > threshold else 0)  # type: ignore[assignment]
    if mask.getbbox() is None:
        return right.copy(), '', False

    mask = mask.filter(ImageFilter.MaxFilter(3))
    mask = mask.filter(ImageFilter.GaussianBlur(radius=DIFF_MASK_FEATHER_RADIUS))
    alpha_mask = mask.point(lambda p: int(round(p * DIFF_MASK_OPACITY)))
    overlay = Image.new('RGBA', right.size, (255, 0, 0, 0))
    overlay.putalpha(alpha_mask)
    marked = Image.alpha_composite(right.convert('RGBA'), overlay).convert('RGB')
    return marked, f'визуальные изменения: mean diff={mean:.2f}, threshold={threshold}, opacity={DIFF_MASK_OPACITY}', True


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
