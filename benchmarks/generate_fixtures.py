#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import fitz

PAGE_SIZES = {
    'a4': fitz.paper_rect('a4'),
    'a1': fitz.paper_rect('a1'),
    'a0': fitz.paper_rect('a0'),
}


def draw_cad_page(page: fitz.Page, page_number: int, changed: bool) -> None:
    rect = page.rect
    margin = min(rect.width, rect.height) * 0.035
    content = fitz.Rect(margin, margin, rect.width - margin, rect.height - margin)
    page.draw_rect(content, color=(0, 0, 0), width=0.6)

    columns = 32
    rows = 24
    for index in range(1, columns):
        x = content.x0 + content.width * index / columns
        page.draw_line((x, content.y0), (x, content.y1), color=(0.72, 0.72, 0.72), width=0.18)
    for index in range(1, rows):
        y = content.y0 + content.height * index / rows
        page.draw_line((content.x0, y), (content.x1, y), color=(0.72, 0.72, 0.72), width=0.18)

    inset = min(content.width, content.height) * 0.08
    page.draw_rect(fitz.Rect(content.x0 + inset, content.y0 + inset, content.x1 - inset, content.y1 - inset), color=(0, 0, 0), width=0.8)
    page.draw_line((content.x0 + inset, content.y0 + inset), (content.x1 - inset, content.y1 - inset), color=(0, 0, 0), width=0.5)
    page.draw_line((content.x0 + inset, content.y1 - inset), (content.x1 - inset, content.y0 + inset), color=(0, 0, 0), width=0.5)

    radius = min(content.width, content.height) * 0.12
    center = fitz.Point(content.x0 + content.width * 0.33, content.y0 + content.height * 0.42)
    page.draw_circle(center, radius, color=(0, 0, 0), width=0.7)
    page.draw_circle(center, radius * 0.55, color=(0, 0, 0), width=0.4)

    title_height = max(36, content.height * 0.08)
    title = fitz.Rect(content.x1 - content.width * 0.34, content.y1 - title_height, content.x1, content.y1)
    page.draw_rect(title, color=(0, 0, 0), width=0.6)
    font_size = max(6, min(18, title_height * 0.20))
    page.insert_text((title.x0 + 6, title.y0 + font_size + 4), f'CAD BASELINE / SHEET {page_number + 1}', fontsize=font_size, color=(0, 0, 0))

    if changed:
        change_x = content.x0 + content.width * 0.68
        change_y = content.y0 + content.height * 0.30
        page.draw_line((change_x, change_y), (change_x + content.width * 0.12, change_y + content.height * 0.08), color=(0, 0, 0), width=1.2)
        page.draw_rect(fitz.Rect(change_x, change_y, change_x + content.width * 0.08, change_y + content.height * 0.06), color=(0, 0, 0), width=1.0)
        page.insert_text((change_x, change_y - 5), 'REV B', fontsize=font_size, color=(0, 0, 0))


def create_pdf(path: Path, page_size: fitz.Rect, pages: int, changed: bool) -> None:
    document = fitz.open()
    for page_number in range(pages):
        page = document.new_page(width=page_size.width, height=page_size.height)
        draw_cad_page(page, page_number, changed=changed)
    document.save(path, garbage=4, deflate=True)
    document.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    cases = [
        ('a4_1', 'a4', 1),
        ('a4_10', 'a4', 10),
        ('a1_1', 'a1', 1),
        ('a0_1', 'a0', 1),
    ]
    for name, size, pages in cases:
        left = output / f'{name}_left.pdf'
        same = output / f'{name}_same.pdf'
        changed = output / f'{name}_changed.pdf'
        create_pdf(left, PAGE_SIZES[size], pages, changed=False)
        shutil.copy2(left, same)
        create_pdf(changed, PAGE_SIZES[size], pages, changed=True)
        print(f'{name}: pages={pages} left={left.stat().st_size} same={same.stat().st_size} changed={changed.stat().st_size}')


if __name__ == '__main__':
    main()
