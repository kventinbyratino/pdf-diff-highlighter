# PDF Compare Highlighter

Минимальное Flask-приложение для сравнения двух многостраничных PDF.

## Что умеет
- сравнение текста по страницам в таблице `Исходный / Измененный`;
- показывает только добавленные и удалённые строки;
- один diff-рендер на страницу;
- полноэкранный предпросмотр diff;
- скачивание diff PNG.

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
