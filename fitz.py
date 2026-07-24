from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from PIL import Image
from pypdf import PdfReader
import pypdfium2
from pypdfium2 import PdfBitmap, PdfDocument as _PdfiumDocument
from reportlab.lib import colors as rl_colors
from reportlab.lib.pagesizes import A0 as rl_A0, A1 as rl_A1, A4 as rl_A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas

version = ("5.12.0", "pdfium-wrapper")

_A4 = (float(rl_A4[0]), float(rl_A4[1]))
_A1 = (float(rl_A1[0]), float(rl_A1[1]))
_A0 = (float(rl_A0[0]), float(rl_A0[1]))


@dataclass(frozen=True)
class Point:
    x: float
    y: float


@dataclass(frozen=True)
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0


@dataclass(frozen=True)
class Matrix:
    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    e: float = 0.0
    f: float = 0.0


class Pixmap:
    def __init__(self, image: Image.Image) -> None:
        self._image = image
        self.width, self.height = image.size
        self.samples = image.convert('RGB').tobytes()

    def tobytes(self, fmt: str = 'png') -> bytes:
        buf = io.BytesIO()
        self._image.save(buf, format=fmt.upper())
        return buf.getvalue()


class TextPage:
    def __init__(self, page: 'Page') -> None:
        self._page = page
        self._textpage = page._page.get_textpage() if page._page is not None else None

    def get_text_range(self, index: int = 0, count: int = -1, errors: str = 'ignore') -> str:
        if self._textpage is None:
            return ''
        return self._textpage.get_text_range(index=index, count=count, errors=errors)

    def close(self) -> None:
        if self._textpage is not None:
            self._textpage.close()
            self._textpage = None


class _BasePage:
    def __init__(self, parent: 'PdfDocumentBase', width: float, height: float) -> None:
        self.parent = parent
        self._width = float(width)
        self._height = float(height)

    @property
    def rect(self) -> Rect:
        return Rect(0.0, 0.0, self._width, self._height)

    def get_width(self) -> float:
        return self._width

    def get_height(self) -> float:
        return self._height

    def get_size(self) -> tuple[float, float]:
        return self._width, self._height


class _LoadedPage(_BasePage):
    def __init__(self, parent: 'PdfDocumentBase', page: pypdfium2.PdfPage) -> None:
        width, height = page.get_size()
        super().__init__(parent, width, height)
        self._page = page

    def get_pixmap(self, matrix: Matrix | None = None, alpha: bool = False) -> Pixmap:
        scale = matrix.a if matrix is not None else 1.0
        bitmap = self._page.render(scale=scale)
        image = bitmap.to_pil()
        bitmap.close()
        if alpha:
            image = image.convert('RGBA')
        return Pixmap(image)

    def get_text(self, kind: str = 'text') -> str:
        if kind != 'text':
            raise ValueError('Only text extraction is supported')
        text_page = self._page.get_textpage()
        try:
            return text_page.get_text_range().strip()
        finally:
            text_page.close()

    def get_textpage(self) -> TextPage:
        return TextPage(self)

    def close(self) -> None:
        self._page.close()


@dataclass
class _DrawOp:
    kind: str
    data: dict[str, Any]


class _WritablePage(_BasePage):
    def __init__(self, parent: 'PdfDocumentBase', width: float, height: float) -> None:
        super().__init__(parent, width, height)
        self._ops: list[_DrawOp] = []

    def _append(self, kind: str, **data: Any) -> None:
        self._ops.append(_DrawOp(kind, data))

    def insert_text(self, pos: tuple[float, float] | Point, text: str, *, fontsize: float = 12, color=(0, 0, 0)) -> None:
        x, y = _point(pos)
        self._append('text', x=x, y=y, text=text, fontsize=fontsize, color=color)

    def draw_rect(
        self,
        rect: Rect,
        *,
        color=(0, 0, 0),
        fill=None,
        width: float = 1.0,
    ) -> None:
        self._append('rect', rect=_rect_tuple(rect), color=color, fill=fill, width=width)

    def draw_line(
        self,
        start: tuple[float, float] | Point,
        end: tuple[float, float] | Point,
        *,
        color=(0, 0, 0),
        width: float = 1.0,
    ) -> None:
        self._append('line', start=_point(start), end=_point(end), color=color, width=width)

    def draw_circle(self, center: tuple[float, float] | Point, radius: float, *, color=(0, 0, 0), width: float = 1.0, fill=None) -> None:
        self._append('circle', center=_point(center), radius=radius, color=color, width=width, fill=fill)

    def draw_polyline(self, points: Iterable[tuple[float, float] | Point], *, color=(0, 0, 0), width: float = 1.0) -> None:
        self._append('polyline', points=[_point(point) for point in points], color=color, width=width)

    def insert_image(self, rect: Rect, *, stream: bytes) -> None:
        self._append('image', rect=_rect_tuple(rect), stream=stream)

    def show_pdf_page(
        self,
        rect: Rect,
        source_doc: 'PdfDocumentBase',
        page_num: int,
        *,
        rotate: float = 0.0,
        keep_proportion: bool = False,
    ) -> None:
        del keep_proportion
        self._append('show_pdf_page', rect=_rect_tuple(rect), source_doc=source_doc, page_num=page_num, rotate=rotate)

    def _draw(self, canvas: rl_canvas.Canvas) -> None:
        for op in self._ops:
            if op.kind == 'text':
                canvas.setFillColorRGB(*_rgb(op.data['color']))
                canvas.setFont('Helvetica', float(op.data['fontsize']))
                canvas.drawString(float(op.data['x']), float(op.data['y']), str(op.data['text']))
            elif op.kind == 'rect':
                x0, y0, x1, y1 = op.data['rect']
                width = float(op.data['width'])
                color = op.data['color']
                fill = op.data['fill']
                canvas.setLineWidth(width)
                canvas.setStrokeColorRGB(*_rgb(color))
                if fill is not None:
                    canvas.setFillColorRGB(*_rgb(fill))
                    canvas.rect(x0, y0, x1 - x0, y1 - y0, stroke=1, fill=1)
                else:
                    canvas.rect(x0, y0, x1 - x0, y1 - y0, stroke=1, fill=0)
            elif op.kind == 'line':
                canvas.setStrokeColorRGB(*_rgb(op.data['color']))
                canvas.setLineWidth(float(op.data['width']))
                x0, y0 = op.data['start']
                x1, y1 = op.data['end']
                canvas.line(x0, y0, x1, y1)
            elif op.kind == 'circle':
                canvas.setStrokeColorRGB(*_rgb(op.data['color']))
                canvas.setLineWidth(float(op.data['width']))
                fill = op.data['fill']
                if fill is not None:
                    canvas.setFillColorRGB(*_rgb(fill))
                    stroke = 1
                    fill_flag = 1
                else:
                    stroke = 1
                    fill_flag = 0
                cx, cy = op.data['center']
                radius = float(op.data['radius'])
                canvas.circle(cx, cy, radius, stroke=stroke, fill=fill_flag)
            elif op.kind == 'polyline':
                canvas.setStrokeColorRGB(*_rgb(op.data['color']))
                canvas.setLineWidth(float(op.data['width']))
                points = op.data['points']
                if len(points) >= 2:
                    path = canvas.beginPath()
                    first_x, first_y = points[0]
                    path.moveTo(first_x, first_y)
                    for x, y in points[1:]:
                        path.lineTo(x, y)
                    canvas.drawPath(path, stroke=1, fill=0)
            elif op.kind == 'image':
                x0, y0, x1, y1 = op.data['rect']
                image = ImageReader(io.BytesIO(op.data['stream']))
                canvas.drawImage(image, x0, y0, width=x1 - x0, height=y1 - y0, preserveAspectRatio=False, mask='auto')
            elif op.kind == 'show_pdf_page':
                x0, y0, x1, y1 = op.data['rect']
                source_doc = op.data['source_doc']
                page_num = int(op.data['page_num'])
                rotate = float(op.data['rotate'])
                image = source_doc._render_page_image(page_num, scale=2.0)
                image_reader = ImageReader(image)
                canvas.saveState()
                if rotate:
                    cx = x0 + (x1 - x0) / 2.0
                    cy = y0 + (y1 - y0) / 2.0
                    canvas.translate(cx, cy)
                    canvas.rotate(rotate)
                    canvas.drawImage(image_reader, -(x1 - x0) / 2.0, -(y1 - y0) / 2.0, width=(x1 - x0), height=(y1 - y0), preserveAspectRatio=False, mask='auto')
                else:
                    canvas.drawImage(image_reader, x0, y0, width=x1 - x0, height=y1 - y0, preserveAspectRatio=False, mask='auto')
                canvas.restoreState()
            else:
                raise NotImplementedError(op.kind)


class PdfDocumentBase:
    needs_pass: bool = False
    page_count: int = 0

    def _render_page_image(self, page_num: int, scale: float = 1.0) -> Image.Image:
        raise NotImplementedError


class PdfDocument(PdfDocumentBase):
    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._loaded_doc: _PdfiumDocument | None = None
        self._pages: list[_WritablePage] = []
        self.needs_pass = False
        if path is not None:
            self._load_existing(Path(path))

    def _load_existing(self, path: Path) -> None:
        try:
            reader = PdfReader(str(path))
            self.needs_pass = bool(reader.is_encrypted)
        except Exception as exc:
            raise PdfiumError(f'Не удалось открыть PDF: {path}') from exc

        if self.needs_pass:
            self.page_count = 0
            return

        self._loaded_doc = _PdfiumDocument(str(path))
        self.page_count = len(self._loaded_doc)

    def __len__(self) -> int:
        return self.page_count

    def __getitem__(self, index: int) -> _LoadedPage:
        if self._loaded_doc is None:
            raise TypeError('Writable PDF does not support page access before save')
        if index < 0 or index >= len(self._loaded_doc):
            raise IndexError(index)
        return _LoadedPage(self, self._loaded_doc.get_page(index))

    def new_page(self, width: float | None = None, height: float | None = None) -> _WritablePage:
        if width is None or height is None:
            width, height = _A4
        page = _WritablePage(self, width, height)
        self._pages.append(page)
        self.page_count = len(self._pages)
        return page

    def save(self, path: str | Path, **kwargs: Any) -> None:
        del kwargs
        if not self._pages:
            raise ValueError('Нельзя сохранить PDF без страниц')
        output_path = str(path)
        c = rl_canvas.Canvas(output_path, pagesize=(_A4[0], _A4[1]))
        for index, page in enumerate(self._pages):
            c.setPageSize((page.get_width(), page.get_height()))
            page._draw(c)
            if index < len(self._pages) - 1:
                c.showPage()
        c.save()

    def close(self) -> None:
        if self._loaded_doc is not None:
            self._loaded_doc.close()
            self._loaded_doc = None

    def __enter__(self) -> 'PdfDocument':
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _render_page_image(self, page_num: int, scale: float = 1.0) -> Image.Image:
        if self._loaded_doc is None:
            raise TypeError('Cannot render pages from writable document')
        page = self._loaded_doc.get_page(page_num)
        try:
            bitmap = page.render(scale=scale)
            image = bitmap.to_pil().convert('RGB')
            bitmap.close()
            return image
        finally:
            page.close()


class PdfiumError(RuntimeError):
    pass


def open(path: str | Path | None = None) -> PdfDocument:
    return PdfDocument(path)


def paper_rect(name: str) -> Rect:
    key = name.lower()
    if key == 'a4':
        w, h = _A4
    elif key == 'a1':
        w, h = _A1
    elif key == 'a0':
        w, h = _A0
    else:
        raise ValueError(f'Unsupported paper size: {name}')
    return Rect(0.0, 0.0, w, h)


def _point(value: tuple[float, float] | Point) -> tuple[float, float]:
    if isinstance(value, Point):
        return float(value.x), float(value.y)
    return float(value[0]), float(value[1])


def _rect_tuple(rect: Rect) -> tuple[float, float, float, float]:
    return float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)


def _rgb(color: Any) -> tuple[float, float, float]:
    if isinstance(color, tuple) and len(color) >= 3:
        return float(color[0]), float(color[1]), float(color[2])
    if isinstance(color, list) and len(color) >= 3:
        return float(color[0]), float(color[1]), float(color[2])
    if color is None:
        return 0.0, 0.0, 0.0
    if hasattr(color, 'red') and hasattr(color, 'green') and hasattr(color, 'blue'):
        return float(color.red), float(color.green), float(color.blue)
    if color == rl_colors.black:
        return 0.0, 0.0, 0.0
    raise TypeError(f'Unsupported color value: {color!r}')


Page = _LoadedPage
Document = PdfDocument
