# PDF Compare Highlighter

Минимальное Flask-приложение для сравнения двух многостраничных PDF.

## Что умеет
- сравнение текста по страницам;
- визуальное сравнение страниц PDF;
- подсветка изменений в HTML;
- загрузка двух файлов через веб-форму.

## Запуск

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Откройте `http://127.0.0.1:8000`.

## Тест

```bash
pytest -q
```
