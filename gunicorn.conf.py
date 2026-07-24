from __future__ import annotations

import os

bind = f"127.0.0.1:{os.environ.get('PORT', '8000')}"
workers = 1
threads = 2
worker_class = 'gthread'
timeout = 180
graceful_timeout = 30
max_requests = 100
max_requests_jitter = 20
accesslog = '-'
errorlog = '-'
capture_output = True
loglevel = 'info'
