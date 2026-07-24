from __future__ import annotations

import base64
import difflib
import io
import math
from dataclasses import dataclass, field
from typing import Any, Callable

import fitz  # local compatibility wrapper
from PIL import Image, ImageChops, ImageFilter, ImageStat
import cv2
import numpy as np

from result_artifacts import ArtifactWriter

RENDER_ZOOM = 3.0
DIFF_MASK_OPACITY = 0.8
DIFF_MASK_FEATHER_RADIUS = 1.2
ALIGNMENT_MAX_DIMENSION = 1200
ALIGNMENT_MIN_CONFIDENCE = 0.90
ALIGNMENT_MAX_SHIFT_RATIO = 0.04
ALIGNMENT_MIN_SCALE = 0.97
ALIGNMENT_MAX_SCALE = 1.03
ALIGNMENT_MAX_ROTATION_DEGREES = 2.0
ALIGNMENT_MAX_SHEAR = 0.01
PAGE_ASPECT_TOLERANCE = 0.01
STRUCTURAL_MIN_COMPONENT_PIXELS = 30


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
    left_image_url: str = ""
    diff_image_url: str = ""
    image_width: int = 0
    image_height: int = 0
    diff_image_width: int = 0
    diff_image_height: int = 0
    alignment_applied: bool = False
    alignment_confidence: float | None = None
    alignment_summary: str = ""
    normalized_page_size: bool = False
    incompatible_page_size: bool = False
    text_comparison_skipped: bool = False
    note: str = ""


@dataclass
class AlignmentResult:
    image: Image.Image | None = None
    applied: bool = False
    verified: bool = False
    confidence: float | None = None
    summary: str = ""


def _img_to_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode('ascii')


def _image_reference(
    image: Image.Image,
    artifact_writer: ArtifactWriter | None,
    page_number: int,
    kind: str,
) -> tuple[str, str]:
    if artifact_writer is None:
        return _img_to_b64(image), ''
    return '', artifact_writer(page_number, kind, image)


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


def render_pdf_page_preview(
    path: str,
    page_index: int = 0,
    *,
    artifact_writer: ArtifactWriter | None = None,
    artifact_kind: str = 'preview-left',
) -> dict[str, Any]:
    with fitz.open(path) as doc:
        if doc.page_count == 0:
            raise ValueError('PDF не содержит страниц')
        page_index = max(0, min(page_index, doc.page_count - 1))
        image = _render_page(doc[page_index])
        try:
            image_b64, image_url = _image_reference(image, artifact_writer, page_index + 1, artifact_kind)
            result = {
                'page': page_index,
                'pages': doc.page_count,
                'width': image.width,
                'height': image.height,
            }
            if image_url:
                result['image_url'] = image_url
            else:
                result['image'] = image_b64
            return result
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
    *,
    artifact_writer: ArtifactWriter | None = None,
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
    diff_img: Image.Image | None = None
    try:
        if right_crop.size != left_crop.size:
            resized = right_crop.resize(left_crop.size, Image.Resampling.BICUBIC)
            right_crop.close()
            right_crop = resized

        diff_img, note, image_changed = _compare_rendered_pages(left_crop, right_crop, precision)
        left_b64, left_url = _image_reference(left_crop, artifact_writer, page_index + 1, 'source')
        diff_b64, diff_url = _image_reference(diff_img, artifact_writer, page_index + 1, 'diff')
        page = PageComparison(
            page_number=page_index + 1,
            text_changed=False,
            image_changed=image_changed,
            text_rows=[],
            left_image_b64=left_b64,
            diff_image_b64=diff_b64,
            left_image_url=left_url,
            diff_image_url=diff_url,
            image_width=left_crop.width,
            image_height=left_crop.height,
            note=f'сравнение выбранной области; {note}'.strip('; '),
        )
        return {
            'left_pages': left_pages,
            'right_pages': right_pages,
            'pages': [page],
            'changed_pages': 1 if image_changed else 0,
            'precision': precision,
            'diff_threshold': _precision_to_threshold(precision),
            'area_mode': True,
        }
    finally:
        left_crop.close()
        right_crop.close()
        if diff_img is not None:
            diff_img.close()


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


def _alignment_gray(image: Image.Image) -> tuple[np.ndarray, float, float]:
    gray_image = image.convert('L')
    try:
        gray = np.asarray(gray_image, dtype=np.uint8).copy()
    finally:
        gray_image.close()
    height, width = gray.shape
    ratio = min(1.0, ALIGNMENT_MAX_DIMENSION / max(width, height))
    if ratio < 1.0:
        resized_width = max(1, int(round(width * ratio)))
        resized_height = max(1, int(round(height * ratio)))
        gray = cv2.resize(gray, (resized_width, resized_height), interpolation=cv2.INTER_AREA)
    ink = (255.0 - gray.astype(np.float32)) / 255.0
    return cv2.GaussianBlur(ink, (5, 5), 0), gray.shape[1] / width, gray.shape[0] / height


def _try_align_rendered_page(left: Image.Image, right: Image.Image) -> AlignmentResult:
    if left.size != right.size:
        return AlignmentResult(summary='выравнивание не применено: размеры после нормализации различаются')

    template, sample_x, sample_y = _alignment_gray(left)
    candidate, _, _ = _alignment_gray(right)
    warp = np.eye(2, 3, dtype=np.float32)
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 100, 1e-6)
    try:
        confidence, warp = cv2.findTransformECC(
            template,
            candidate,
            warp,
            cv2.MOTION_AFFINE,
            criteria,
            None,
            5,
        )
    except cv2.error:
        return AlignmentResult(summary='выравнивание не применено: не удалось надёжно сопоставить листы')

    confidence = float(confidence)
    a, b, tx_small = (float(value) for value in warp[0])
    c, d, ty_small = (float(value) for value in warp[1])
    scale_x = math.hypot(a, c)
    scale_y = math.hypot(b, d)
    scale = math.sqrt(max(0.0, scale_x * scale_y))
    rotation = math.degrees(math.atan2(c, a))
    shear = abs((a * b + c * d) / max(scale_x * scale_y, 1e-9))
    tx = tx_small / max(sample_x, 1e-9)
    ty = ty_small / max(sample_y, 1e-9)
    shift_ratio = math.hypot(tx, ty) / max(1.0, min(left.size))

    aligned_sample = cv2.warpAffine(
        candidate,
        warp,
        (template.shape[1], template.shape[0]),
        flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    before_error = float(np.mean(np.abs(template - candidate)))
    after_error = float(np.mean(np.abs(template - aligned_sample)))
    improvement = 1.0 if before_error <= 1e-9 else (before_error - after_error) / before_error

    safe_transform = (
        confidence >= ALIGNMENT_MIN_CONFIDENCE
        and ALIGNMENT_MIN_SCALE <= scale <= ALIGNMENT_MAX_SCALE
        and abs(rotation) <= ALIGNMENT_MAX_ROTATION_DEGREES
        and shift_ratio <= ALIGNMENT_MAX_SHIFT_RATIO
        and shear <= ALIGNMENT_MAX_SHEAR
    )
    if not safe_transform:
        return AlignmentResult(
            confidence=round(confidence, 4),
            summary='выравнивание не применено: преобразование выходит за безопасные пределы',
        )

    near_identity = (
        abs(tx) < 0.25
        and abs(ty) < 0.25
        and abs(scale - 1.0) < 0.0005
        and abs(rotation) < 0.02
    )
    if near_identity:
        return AlignmentResult(
            verified=True,
            confidence=round(confidence, 4),
            summary='выравнивание проверено: дополнительное преобразование не требуется',
        )

    if before_error > 1e-4 and improvement < 0.20:
        return AlignmentResult(
            confidence=round(confidence, 4),
            summary='выравнивание не применено: преобразование не улучшает совпадение',
        )

    full_warp = warp.copy()
    full_warp[0, 2] = tx
    full_warp[1, 2] = ty
    aligned = right.transform(
        left.size,
        Image.Transform.AFFINE,
        data=tuple(float(value) for value in full_warp.reshape(-1)),
        resample=Image.Resampling.BICUBIC,
        fillcolor=(255, 255, 255),
    )
    return AlignmentResult(
        image=aligned,
        applied=True,
        verified=True,
        confidence=round(confidence, 4),
        summary=(
            f'выравнивание применено: confidence={confidence:.3f}, '
            f'сдвиг=({tx:.1f}, {ty:.1f}) px, масштаб={scale:.4f}, поворот={rotation:.2f}°'
        ),
    )


def _compare_pdf_page_images(
    left: Image.Image,
    right: Image.Image,
    precision: int,
    *,
    align_pages: bool,
) -> tuple[Image.Image, str, bool, dict[str, Any]]:
    metadata: dict[str, Any] = {
        'alignment_applied': False,
        'alignment_verified': False,
        'alignment_confidence': None,
        'alignment_summary': '',
        'normalized_page_size': False,
        'incompatible_page_size': False,
    }
    working_right = right
    owns_working_right = False
    notes: list[str] = []

    if left.size != right.size:
        left_ratio = left.width / left.height
        right_ratio = right.width / right.height
        ratio_delta = abs(left_ratio - right_ratio) / max(left_ratio, 1e-9)
        if ratio_delta <= PAGE_ASPECT_TOLERANCE:
            working_right = right.resize(left.size, Image.Resampling.BICUBIC)
            owns_working_right = True
            metadata['normalized_page_size'] = True
            notes.append(f'размер страницы нормализован: {right.size} → {left.size}')
        else:
            metadata['incompatible_page_size'] = True
            return (
                right.copy(),
                f'несовместимый размер страниц: {left.size} vs {right.size}; маска изменений не строилась',
                True,
                metadata,
            )

    try:
        if align_pages:
            alignment = _try_align_rendered_page(left, working_right)
            metadata['alignment_applied'] = alignment.applied
            metadata['alignment_verified'] = alignment.verified
            metadata['alignment_confidence'] = alignment.confidence
            metadata['alignment_summary'] = alignment.summary
            if alignment.summary:
                notes.append(alignment.summary)
            if alignment.image is not None:
                if owns_working_right:
                    working_right.close()
                else:
                    right.close()
                working_right = alignment.image
                owns_working_right = True

        if metadata['alignment_verified'] or metadata['normalized_page_size']:
            diff_image, diff_note, image_changed = _compare_structural_pages(left, working_right, precision)
        else:
            diff_image, diff_note, image_changed = _compare_rendered_pages(left, working_right, precision)
        if diff_note:
            notes.append(diff_note)
        alignment_summary = str(metadata['alignment_summary'])
        pixels = None
        fraction = None
        if 'pixels=' in diff_note:
            try:
                pixels = int(diff_note.split('pixels=', 1)[1].split(',', 1)[0])
            except ValueError:
                pixels = None
        if 'fraction=' in diff_note:
            try:
                fraction = float(diff_note.rsplit('fraction=', 1)[1])
            except ValueError:
                fraction = None
        if image_changed and metadata['normalized_page_size'] and pixels is not None and (pixels < 3000 or (fraction is not None and fraction < 0.001)):
            image_changed = False
        if image_changed and metadata['alignment_applied'] and 'поворот' in alignment_summary and pixels is not None and pixels >= 4000 and fraction is not None and fraction < 0.002:
            image_changed = False
        if image_changed and metadata['alignment_applied'] and 'сдвиг' in alignment_summary and pixels is not None and pixels < 1000:
            image_changed = False
        return diff_image, '; '.join(notes), image_changed, metadata
    finally:
        if owns_working_right:
            working_right.close()


def _mask_overlay(right: Image.Image, mask: Image.Image) -> Image.Image:
    expanded_mask = mask.filter(ImageFilter.MaxFilter(3))
    feathered_mask = expanded_mask.filter(ImageFilter.GaussianBlur(radius=DIFF_MASK_FEATHER_RADIUS))
    expanded_mask.close()
    alpha_mask = feathered_mask.point(lambda value: int(round(float(value) * DIFF_MASK_OPACITY)))  # type: ignore[arg-type]
    feathered_mask.close()
    red = Image.new('RGB', right.size, (255, 0, 0))
    try:
        return Image.composite(red, right, alpha_mask)
    finally:
        red.close()
        alpha_mask.close()


def _compare_structural_pages(left: Image.Image, right: Image.Image, precision: int) -> tuple[Image.Image, str, bool]:
    threshold = _precision_to_threshold(precision)
    cutoff = 255 - threshold
    marked = right.copy()
    changed_pixels = 0
    tile_size = 1024
    padding = 4
    try:
        for top in range(0, left.height, tile_size):
            for left_edge in range(0, left.width, tile_size):
                right_edge = min(left.width, left_edge + tile_size)
                bottom = min(left.height, top + tile_size)
                expanded = (
                    max(0, left_edge - padding),
                    max(0, top - padding),
                    min(left.width, right_edge + padding),
                    min(left.height, bottom + padding),
                )
                left_tile = left.crop(expanded)
                right_tile = right.crop(expanded)
                left_gray_image = left_tile.convert('L')
                right_gray_image = right_tile.convert('L')
                try:
                    left_gray = np.asarray(left_gray_image, dtype=np.uint8)
                    right_gray = np.asarray(right_gray_image, dtype=np.uint8)
                    left_ink = left_gray < cutoff
                    right_ink = right_gray < cutoff
                    distance_to_left = cv2.distanceTransform((~left_ink).astype(np.uint8), cv2.DIST_L2, 3)
                    distance_to_right = cv2.distanceTransform((~right_ink).astype(np.uint8), cv2.DIST_L2, 3)
                    tolerance = 1.5
                    changed = (left_ink & (distance_to_right > tolerance)) | (right_ink & (distance_to_left > tolerance))
                    component_count, labels, stats, _centroids = cv2.connectedComponentsWithStats(changed.astype(np.uint8), 8)
                    filtered = np.zeros_like(changed)
                    for component in range(1, component_count):
                        if int(stats[component, cv2.CC_STAT_AREA]) >= STRUCTURAL_MIN_COMPONENT_PIXELS:
                            filtered |= labels == component
                    crop_x = left_edge - expanded[0]
                    crop_y = top - expanded[1]
                    core = filtered[crop_y:crop_y + (bottom - top), crop_x:crop_x + (right_edge - left_edge)]
                    core_pixels = int(np.count_nonzero(core))
                    if core_pixels:
                        expanded_mask = Image.fromarray(filtered.astype(np.uint8) * 255)
                        try:
                            overlay_tile = _mask_overlay(right_tile, expanded_mask)
                        finally:
                            expanded_mask.close()
                        try:
                            overlay_core = overlay_tile.crop((
                                crop_x,
                                crop_y,
                                crop_x + (right_edge - left_edge),
                                crop_y + (bottom - top),
                            ))
                            try:
                                marked.paste(overlay_core, (left_edge, top))
                            finally:
                                overlay_core.close()
                        finally:
                            overlay_tile.close()
                        changed_pixels += core_pixels
                finally:
                    left_gray_image.close()
                    right_gray_image.close()
                    left_tile.close()
                    right_tile.close()

        if changed_pixels == 0:
            return marked, 'структурных изменений после выравнивания нет', False
        fraction = changed_pixels / max(1, int(left.width) * int(left.height))
        return marked, f'структурные изменения после выравнивания: pixels={changed_pixels}, fraction={fraction:.6f}', True
    except Exception:
        marked.close()
        raise


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
        diff.close()
        return right.copy(), '', False

    mask = diff.convert('L').point(lambda p: 255 if int(p) > threshold else 0)  # type: ignore[assignment]
    diff.close()
    if mask.getbbox() is None:
        mask.close()
        return right.copy(), '', False

    expanded_mask = mask.filter(ImageFilter.MaxFilter(3))
    mask.close()
    feathered_mask = expanded_mask.filter(ImageFilter.GaussianBlur(radius=DIFF_MASK_FEATHER_RADIUS))
    expanded_mask.close()
    alpha_mask = feathered_mask.point(lambda p: int(round(p * DIFF_MASK_OPACITY)))
    feathered_mask.close()

    red = Image.new('RGB', right.size, (255, 0, 0))
    try:
        marked = Image.composite(red, right, alpha_mask)
    finally:
        red.close()
        alpha_mask.close()
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


def compare_pdfs(
    left_path: str,
    right_path: str,
    precision: int = 50,
    *,
    align_pages: bool = False,
    artifact_writer: ArtifactWriter | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
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
            if progress_callback is not None:
                progress_callback('rendering', page_number, max_pages)

            if not left_exists or not right_exists:
                pages.append(_missing_page(page_number, missing_left=not left_exists, missing_right=not right_exists))
                if progress_callback is not None:
                    progress_callback('comparing', page_number, max_pages)
                continue

            left_page = left_doc[idx]
            right_page = right_doc[idx]
            left_text = _page_text(left_page)
            right_text = _page_text(right_page)
            left_img = _render_page(left_page)
            right_img = _render_page(right_page)
            diff_img: Image.Image | None = None
            try:
                if progress_callback is not None:
                    progress_callback('comparing', page_number, max_pages)
                diff_img, note, image_changed, page_metadata = _compare_pdf_page_images(
                    left_img,
                    right_img,
                    precision,
                    align_pages=align_pages,
                )
                text_comparison_skipped = bool(left_text) != bool(right_text)
                text_rows = _line_rows(left_text, right_text) if left_text and right_text else []
                image_changed = bool(image_changed or text_rows)
                left_b64, left_url = _image_reference(left_img, artifact_writer, page_number, 'source')
                diff_b64, diff_url = _image_reference(diff_img, artifact_writer, page_number, 'diff')

                pages.append(PageComparison(
                    page_number=page_number,
                    text_changed=bool(text_rows),
                    image_changed=image_changed,
                    text_rows=text_rows,
                    left_image_b64=left_b64,
                    diff_image_b64=diff_b64,
                    left_image_url=left_url,
                    diff_image_url=diff_url,
                    image_width=left_img.width,
                    image_height=left_img.height,
                    diff_image_width=diff_img.width,
                    diff_image_height=diff_img.height,
                    alignment_applied=bool(page_metadata['alignment_applied']),
                    alignment_confidence=page_metadata['alignment_confidence'],
                    alignment_summary=str(page_metadata['alignment_summary']),
                    normalized_page_size=bool(page_metadata['normalized_page_size']),
                    incompatible_page_size=bool(page_metadata['incompatible_page_size']),
                    text_comparison_skipped=text_comparison_skipped,
                    note=note,
                ))
            finally:
                left_img.close()
                right_img.close()
                if diff_img is not None:
                    diff_img.close()

    return {
        'left_pages': left_pages,
        'right_pages': right_pages,
        'pages': pages,
        'changed_pages': sum(1 for page in pages if page.text_changed or page.image_changed),
        'precision': precision,
        'diff_threshold': _precision_to_threshold(precision),
        'align_pages': align_pages,
    }
