"""Gunicorn configuration for GitPulse.

Documented options: https://docs.gunicorn.org/en/stable/settings.html
"""

import multiprocessing
import os

# Bind to the port provided by the platform (Render / Railway / Docker).
bind = f"0.0.0.0:{os.getenv('PORT', '8000')}"

# Workers: a sensible default based on available CPU cores.
workers = int(os.getenv("GUNICORN_WORKERS", multiprocessing.cpu_count() * 2 + 1))

worker_class = "sync"
timeout = 120
graceful_timeout = 30
keepalive = 5

accesslog = "-"   # stdout - visible in `docker logs`
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info").lower()

# Run as the unprivileged user when running in the container.
user = os.getenv("GUNICORN_USER", "gitpulse")
