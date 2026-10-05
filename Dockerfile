# One image for everything Python in production: the dashboard/API, the
# 3-minute sync loop and the daily catalogue refresh. They differ only in the
# command they run (see deploy/docker-compose.yml).
#
# Build context is the repo root. .dockerignore keeps .env, venv, .git,
# tests and "Claude outputs" out of the image, so no secret is ever baked in:
# credentials arrive at runtime from deploy/.env.

FROM python:3.12-slim

# PYTHONUNBUFFERED: print() output reaches `docker compose logs` immediately
#   instead of sitting in a buffer (the sync loop's one-line-per-run log
#   would otherwise appear minutes late, or be lost on a crash).
# PYTHONDONTWRITEBYTECODE: no .pyc files; the app user can't write /app anyway.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, code second: a code-only change reuses the cached
# dependency layer, so a redeploy rebuilds in seconds.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY web ./web

# Run as an unprivileged user. Nothing here needs root, and it limits the
# damage if the app were ever compromised.
RUN useradd --system --uid 10001 --no-create-home storefront
USER storefront

EXPOSE 8000

# --proxy-headers / --forwarded-allow-ips: the only thing that can reach this
# port is the cloudflared container on the private Docker network, so trusting
# its X-Forwarded-* headers is safe and gives correct client IPs in the logs.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips=*"]
