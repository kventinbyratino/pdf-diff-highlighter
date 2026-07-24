# PDF Diff Highlighter on Windows Server with IIS

Это пакет файлов для запуска проекта на другом Windows-сервере, если ты скачиваешь репозиторий с GitHub и хочешь:

- держать Python-приложение локально на `127.0.0.1:8000`;
- открывать сайт через IIS;
- использовать IIS как reverse proxy;
- запускать backend как службу Windows.

## Что входит в пакет

- `web.config` — правило reverse proxy для IIS;
- `install.ps1` — первичная установка Python-зависимостей и backend-службы;
- `run-backend.ps1` — wrapper для запуска Flask через Waitress;
- `start.ps1` — запуск backend-службы;
- `stop.ps1` — остановка backend-службы;
- `update.ps1` — обновление кода после `git pull`.

## Рекомендуемая схема

| Компонент | Роль |
|---|---|
| IIS | внешний HTTP/HTTPS фронт |
| URL Rewrite + ARR | проксирование запросов внутрь |
| Waitress | WSGI-сервер для Flask |
| NSSM | запуск Python-приложения как службы Windows |
| Flask app | слушает только `127.0.0.1:8000` |

## Как развернуть

### 1. Склонировать проект

```powershell
cd C:\apps
git clone https://github.com/<owner>/<repo>.git pdf-diff-highlighter
cd pdf-diff-highlighter
```

### 2. Установить зависимости

```powershell
powershell -ExecutionPolicy Bypass -File .\IIS\install.ps1 -RepoPath C:\apps\pdf-diff-highlighter
```

### 3. Настроить IIS

Этот вариант рассчитан на прямое открытие сайта из корня домена, например:

- `http://your-server/`
- `https://your-domain.ru/`

#### 3.1. Установить IIS

В **Server Manager** открой:

```text
Add roles and features → Server Roles → Web Server (IIS)
```

Минимально включи компоненты:

```text
Web Server
├─ Common HTTP Features
│  ├─ Default Document
│  ├─ Static Content
│  └─ HTTP Errors
├─ Health and Diagnostics
│  └─ HTTP Logging
├─ Security
│  └─ Request Filtering
└─ Management Tools
   └─ IIS Management Console
```

#### 3.2. Установить URL Rewrite и ARR

Установи два модуля Microsoft IIS:

- **URL Rewrite**
- **Application Request Routing (ARR)**

После установки перезапусти **IIS Manager**.

#### 3.3. Включить proxy в ARR

В **IIS Manager**:

1. выбери верхний узел сервера;
2. открой **Application Request Routing Cache**;
3. справа нажми **Server Proxy Settings...**;
4. включи **Enable proxy**;
5. нажми **Apply**.

Без этого шага `web.config` с reverse proxy не заработает.

#### 3.4. Создать отдельную физическую папку сайта

Например:

```powershell
mkdir C:\inetpub\pdf-diff-highlighter
copy C:\apps\pdf-diff-highlighter\IIS\web.config C:\inetpub\pdf-diff-highlighter\web.config
```

В этой папке IIS будет видеть только `web.config`. Сам код проекта остаётся в:

```text
C:\apps\pdf-diff-highlighter
```

#### 3.5. Создать сайт IIS

В **IIS Manager**:

1. открой **Sites**;
2. нажми **Add Website...**;
3. заполни:

| Поле | Значение |
|---|---|
| Site name | `pdf-diff-highlighter` |
| Physical path | `C:\inetpub\pdf-diff-highlighter` |
| Binding type | `http` или `https` |
| Port | `80` для HTTP или `443` для HTTPS |
| Host name | домен, если используется |

Если сайт должен открываться просто по IP/имени сервера, поле **Host name** можно оставить пустым.

#### 3.6. Проверить правило reverse proxy

Файл должен лежать здесь:

```text
C:\inetpub\pdf-diff-highlighter\web.config
```

Содержимое базового правила:

```xml
<action type="Rewrite" url="http://127.0.0.1:8000/{R:1}" />
```

То есть IIS принимает внешний запрос и передаёт его локальному backend на порт `8000`.

#### 3.7. Перезапустить сайт

```powershell
iisreset
```

Или через IIS Manager:

```text
Sites → pdf-diff-highlighter → Restart
```

### 4. Проверить backend

```powershell
curl http://127.0.0.1:8000/health
```

### 5. Проверить сайт через IIS

```powershell
curl http://your-server/health
```

## Переменные окружения backend

Рекомендуемые значения:

```text
PORT=8000
APP_ENVIRONMENT=prod
RESULT_ARTIFACT_ROOT=C:\pdf-diff-data\artifacts
COMPARISON_JOB_ROOT=C:\pdf-diff-data\jobs
USAGE_METRICS_PATH=C:\pdf-diff-data\usage_metrics.json
```

## Если сайт открывается напрямую

Для прямого открытия из корня домена ничего дополнительного не нужно: используйте этот пакет как есть.

## Проверка после обновления

```powershell
powershell -ExecutionPolicy Bypass -File .\IIS\update.ps1 -RepoPath C:\apps\pdf-diff-highlighter
curl http://127.0.0.1:8000/health
```

## Важно

- IIS не запускает Flask напрямую.
- Внешний доступ идёт через IIS.
- Flask-сервис должен слушать только localhost.
- Для этого проекта backend запускается через `waitress`.
