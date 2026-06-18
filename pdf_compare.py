from __future__ import annotations

import base64
import difflib
import html
import io
from dataclasses import dataclass
from typing import Any

import fitz  # PyMuPDF
from PIL import Image, ImageChops, ImageStat


@dataclass
class PageComparison:
    page_number: int
    text_changed: bool
    image_changed: bool
    missing_left: bool = False
    missing_right: bool = False
    text_diff_html: str = ""
    left_image_b64: str = ""
    right_image_b64: str = ""
    diff_image_b64: str = ""
    note: str = ""


def _img_to_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _render_page(page: fitz.Page, zoom: float = 1.5) -> Image.Image:
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _page_text(page: fitz.Page) -> str:
    return page.get_text("text").strip()


def _text_diff_html(left: str, right: str) -> str:
    if left == right:
        return f"<div class='same'>{html.escape(left or '(нет текста)')}</div>"

    left_lines = left.splitlines()
    right_lines = right.splitlines()
    sm = difflib.SequenceMatcher(a=left_lines, b=right_lines)
    out: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            for line in left_lines[i1:i2]:
                out.append(f"<div class='eq'>{html.escape(line)}</div>")
        elif tag == 'delete':
            for line in left_lines[i1:i2]:
                out.append(f"<div class='del'>- {html.escape(line)}</div>")
        elif tag == 'insert':
            for line in right_lines[j1:j2]:
                out.append(f"<div class='ins'>+ {html.escape(line)}</div>")
        else:
            for line in left_lines[i1:i2]:
                out.append(f"<div class='del'>- {html.escape(line)}</div>")
            for line in right_lines[j1:j2]:
                out.append(f"<div class='ins'>+ {html.escape(line)}</div>")
    return "\n".join(out)


def _diff_image(left: Image.Image, right: Image.Image) -> tuple[Image.Image, str]:
    if left.size != right.size:
        canvas = Image.new('RGB', (left.width + right.width + 20, max(left.height, right.height)), 'white')
        canvas.paste(left, (0, 0))
        canvas.paste(right, (left.width + 20, 0))
        return canvas, f'разный размер страниц: {left.size} vs {right.size}'

    diff = ImageChops.difference(left, right)
    stat = ImageStat.Stat(diff)
    mean = sum(stat.mean) / len(stat.mean)
    if diff.getbbox() is None or mean < 1.0:
        return right.copy(), ''

    gray = diff.convert('L')
    mask = gray.point(lambda p: 255 if p > 20 else 0)
    highlight = Image.new('RGB', left.size, (255, 65, 65))
    merged = Image.composite(highlight, right, mask)
    blended = Image.blend(right, merged, 0.55)
    return blended, f'визуальные изменения: mean diff={mean:.2f}'


def compare_pdfs(left_path: str, right_path: str) -> dict[str, Any]:
    left_doc = fitz.open(left_path)
    right_doc = fitz.open(right_path)
    left_pages = left_doc.page_count
    right_pages = right_doc.page_count
    max_pages = max(left_pages, right_pages)
    pages: list[PageComparison] = []

    for idx in range(max_pages):
        left_exists = idx < left_doc.page_count
        right_exists = idx < right_doc.page_count
        page_number = idx + 1

        if not left_exists or not right_exists:
            pages.append(PageComparison(
                page_number=page_number,
                text_changed=True,
                image_changed=True,
                missing_left=not left_exists,
                missing_right=not right_exists,
                note='страница есть только в одном PDF',
            ))
            continue

        lp = left_doc[idx]
        rp = right_doc[idx]
        left_text = _page_text(lp)
        right_text = _page_text(rp)
        left_img = _render_page(lp)
        right_img = _render_page(rp)
        diff_img, note = _diff_image(left_img, right_img)

        text_changed = left_text != right_text
        image_changed = bool(note)
        pages.append(PageComparison(
            page_number=page_number,
            text_changed=text_changed,
            image_changed=image_changed,
            text_diff_html=_text_diff_html(left_text, right_text),
            left_image_b64=_img_to_b64(left_img),
            right_image_b64=_img_to_b64(right_img),
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
    }
