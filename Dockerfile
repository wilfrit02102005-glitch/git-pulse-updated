# ---- Build stage: install dependencies ----
FROM python:3.12-slim AS builder

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- Runtime stage: slim image ----
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    FLASK_ENV=production

# Copy only the installed packages from the builder stage.
COPY --from=builder /install /usr/local

# Create a non-root user for security.
RUN useradd --create-home --shell /usr/sbin/nologin gitpulse \
    && mkdir -p /app/logs \
    && chown -R gitpulse:gitpulse /app

# Copy the application source.
COPY --chown=gitpulse:gitpulse . .

USER gitpulse

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1

# Production entrypoint: gunicorn with the committed config file.
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
