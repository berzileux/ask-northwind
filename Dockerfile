# Slim Python image keeps the Cloud Run cold start short.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Requirements are copied alone first so the dependency layer stays cached
# until requirements.txt changes. The dev requirements are never installed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Fixed numeric ids keep file ownership stable across builds.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app

# What is left out of the image is decided by .dockerignore.
COPY --chown=app:app . .

# The sandbox opens the database read-only and copies it into memory per
# request, so the file never needs to be writable.
RUN if [ -f northwind.db ]; then chmod 444 northwind.db; fi

USER app

# No secret is baked into the image. GEMINI_API_KEY arrives at run time from
# Cloud Run. The application path below must match the module written in the
# web page step. Cloud Run sets PORT; 8080 is the fallback when it is unset.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}"]
