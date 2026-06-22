from __future__ import annotations

import base64
import difflib
import io
from dataclasses import dataclass, field
from typing import Any

import fitz  # PyMuPDF
from PIL import Image, ImageChops, ImageStat

try:
    import mss  # type: ignore
except ImportError:  # pragma: no cover - optional dependency for screen capture
    mss = None  # type: ignore[assignment]


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


@dataclass
class ImageComparison:
    left_size: tuple[int, int]
    right_size: tuple[int, int]
    changed: bool
    diff_image_b64: str = ""
    note: str = ""
    precision: int = 50
    diff_threshold: int = 0


def _img_to_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode('ascii')


def _open_rgb_image(path: str) -> Image.Image:
    with Image.open(path) as img:
        return img.convert('RGB')


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
            for line in left_lines[i1:i2]:
                rows.append(TextChange(source=line, changed='', kind='Удалено'))
        if tag in ('insert', 'replace'):
            for line in right_lines[j1:j2]:
                rows.append(TextChange(source='', changed=line, kind='Добавлено'))
    return rows


def _precision_to_threshold(precision: int) -> int:
    precision = max(1, min(100, int(precision)))
    return max(1, round(35 - (precision - 1) * 34 / 99))


def _compare_image_pair(left: Image.Image, right: Image.Image, precision: int) -> tuple[Image.Image, str, bool, int]:
    precision = max(1, min(100, int(precision)))
    if left.size != right.size:
        canvas = Image.new('RGB', (left.width + right.width + 20, max(left.height, right.height)), 'white')
        canvas.paste(left, (0, 0))
        canvas.paste(right, (left.width + 20, 0))
        return canvas, f'разный размер изображений: {left.size} vs {right.size}', True, _precision_to_threshold(precision)

    diff = ImageChops.difference(left, right)
    stat = ImageStat.Stat(diff)
    mean = sum(stat.mean) / len(stat.mean)
    if diff.getbbox() is None or mean < 1.0:
        return right.copy(), '', False, _precision_to_threshold(precision)

    threshold = _precision_to_threshold(precision)
    gray = diff.convert('L')
    mask = gray.point(lambda p: 255 if int(p) > threshold else 0)  # type: ignore[assignment]
    if mask.getbbox() is None:
        return right.copy(), '', False, threshold

    highlight = Image.new('RGB', left.size, (255, 65, 65))
    merged = Image.composite(highlight, right, mask)
    blended = Image.blend(right, merged, 0.55)
    return blended, f'визуальные изменения: mean diff={mean:.2f}, threshold={threshold}', True, threshold


def compare_images(left_path: str, right_path: str, precision: int = 50) -> ImageComparison:
    left_img = _open_rgb_image(left_path)
    right_img = _open_rgb_image(right_path)
    diff_img, note, changed, threshold = _compare_image_pair(left_img, right_img, precision)
    result = ImageComparison(
        left_size=left_img.size,
        right_size=right_img.size,
        changed=changed,
        diff_image_b64=_img_to_b64(diff_img),
        note=note,
        precision=max(1, min(100, int(precision))),
        diff_threshold=threshold,
    )
    left_img.close()
    right_img.close()
    return result


def compare_pdfs(left_path: str, right_path: str, precision: int = 50) -> dict[str, Any]:
    precision = max(1, min(100, int(precision)))
    left_doc = fitz.open(left_path)
    right_doc = fitz.open(right_path)
    left_pages = left_doc.page_count
    right_pages = right_doc.page_count
    max_pages = max(left_pages, right_pages)
    pages: list[PageComparison] = []
    diff_threshold = _precision_to_threshold(precision)

    for idx in range(max_pages):
        left_exists = idx < left_pages
        right_exists = idx < right_pages
        page_number = idx + 1

        if not left_exists or not right_exists:
            pages.append(PageComparison(
                page_number=page_number,
                text_changed=True,
                image_changed=True,
                missing_left=not left_exists,
                missing_right=not right_exists,
                text_rows=[TextChange(source='(страница отсутствует)', changed='(страница отсутствует)', kind='Страница отличается')],
                note='страница есть только в одном PDF',
            ))
            continue

        lp = left_doc[idx]
        rp = right_doc[idx]
        left_text = _page_text(lp)
        right_text = _page_text(rp)
        left_img = _render_page(lp)
        right_img = _render_page(rp)
        diff_img, note, image_changed, _ = _compare_image_pair(left_img, right_img, precision)

        text_rows = _line_rows(left_text, right_text)
        text_changed = bool(text_rows)
        pages.append(PageComparison(
            page_number=page_number,
            text_changed=text_changed,
            image_changed=image_changed,
            text_rows=text_rows,
            left_image_b64=_img_to_b64(left_img),
            diff_image_b64=_img_to_b64(diff_img),
            note=note,
        ))

    left_doc.close()
    right_doc.close()

    return {
        'left_pages': left_pages,
        'right_pages': right_pages,
        'pages': pages,
        'changed_pages': sum(1 for p in pages if p.text_changed or p.image_changed),
        'precision': precision,
        'diff_threshold': diff_threshold,
    }


def capture_screen_png(monitor: int = 1) -> bytes:
    if mss is None:
        raise RuntimeError('mss не установлен')

    with mss.mss() as sct:
        if len(sct.monitors) <= 1:
            raise RuntimeError('Экран не найден')
        monitor_index = max(1, min(int(monitor), len(sct.monitors) - 1))
        shot = sct.grab(sct.monitors[monitor_index])
        img = Image.frombytes('RGB', shot.size, shot.rgb)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()
