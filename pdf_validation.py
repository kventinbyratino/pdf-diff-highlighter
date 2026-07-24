from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import fitz

MIB = 1024 * 1024
MAX_FILE_BYTES = 25 * MIB
MAX_TOTAL_BYTES = 50 * MIB
MAX_PAGES = 20


class UserFacingError(Exception):
    status_code = 400

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code


class PdfValidationError(UserFacingError):
    status_code = 400


class PdfLimitError(PdfValidationError):
    status_code = 413


@dataclass(frozen=True)
class PdfMetadata:
    path: Path
    size_bytes: int
    pages: int


@dataclass(frozen=True)
class PdfPairMetadata:
    left: PdfMetadata
    right: PdfMetadata
    total_bytes: int


def _validate_pdf(path: Path, label: str) -> PdfMetadata:
    size_bytes = path.stat().st_size
    if size_bytes == 0:
        raise PdfValidationError(f'Файл «{label}» пуст')
    if size_bytes > MAX_FILE_BYTES:
        raise PdfLimitError(f'Файл «{label}» больше 25 МБ')

    with path.open('rb') as file:
        if file.read(5) != b'%PDF-':
            raise PdfValidationError(f'Файл «{label}» не является PDF')

    try:
        document = fitz.open(path)
    except Exception as exc:
        raise PdfValidationError(f'Файл «{label}» повреждён или имеет неподдерживаемый формат') from exc

    try:
        if document.needs_pass:
            raise PdfValidationError(f'Файл «{label}» защищён паролем')
        pages = document.page_count
        if pages == 0:
            raise PdfValidationError(f'Файл «{label}» не содержит страниц')
        if pages > MAX_PAGES:
            raise PdfLimitError(f'Файл «{label}» содержит больше {MAX_PAGES} страниц')
    finally:
        document.close()

    return PdfMetadata(path=path, size_bytes=size_bytes, pages=pages)


def validate_pdf_pair(left_path: Path, right_path: Path) -> PdfPairMetadata:
    left = _validate_pdf(left_path, 'Чертеж 1')
    right = _validate_pdf(right_path, 'Чертеж 2')
    total_bytes = left.size_bytes + right.size_bytes
    if total_bytes > MAX_TOTAL_BYTES:
        raise PdfLimitError('Суммарный размер PDF превышает 50 МБ')
    return PdfPairMetadata(left=left, right=right, total_bytes=total_bytes)
