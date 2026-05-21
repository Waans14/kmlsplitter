# ─────────────────────────────────────────────────────────
#  Multi-stage Dockerfile — KML Splitter Pro
# ─────────────────────────────────────────────────────────

# Stage 1: Build / dependency layer
FROM python:3.12-slim AS builder

WORKDIR /build

# Only copy requirements first (layer caching)
COPY requirements.txt .

RUN pip install --upgrade pip \
    && pip install --no-cache-dir --prefix=/install -r requirements.txt


# Stage 2: Production image
FROM python:3.12-slim AS production

LABEL maintainer="Dinas Kominfo KSB"
LABEL description="KML Splitter Pro — production image"

# Non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser appuser

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY app.py .

# Ensure non-root ownership
RUN chown -R appuser:appuser /app

USER appuser

# Expose Gunicorn port
EXPOSE 8000

# Gunicorn: 2 sync workers, 120s timeout, bind to 0.0.0.0:8000
# Workers and timeout overridable via ENV
CMD ["sh", "-c", \
     "gunicorn app:app \
      --workers ${WORKERS:-2} \
      --timeout ${TIMEOUT:-120} \
      --bind 0.0.0.0:8000 \
      --access-logfile - \
      --error-logfile - \
      --log-level info \
      --preload"]
