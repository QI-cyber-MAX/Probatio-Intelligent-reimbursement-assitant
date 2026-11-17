FROM ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 AS uv
FROM python:3.14.8-slim-bookworm@sha256:44e000a07b48bef1517a2b14adc93c17af728ca98858b5e24c6e1b18de1b2ef2

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH="/opt/ira/.venv/bin:$PATH" \
    HF_HUB_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HOME=/tmp/ira-runtime

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       libreoffice-writer fonts-noto-cjk fontconfig \
       libglib2.0-0 libgl1 libgomp1 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 ira-runtime \
    && useradd --uid 10001 --gid 10001 --no-create-home ira-runtime

COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /opt/ira
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_CACHE_DIR=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY src/ src/
COPY README.md alembic.ini ./
COPY migrations/ migrations/
COPY deploy/app/prepare-storage.sh /usr/local/bin/prepare-storage
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_CACHE_DIR=/root/.cache/uv uv sync --locked --no-dev --no-editable \
    && chmod 755 /usr/local/bin/prepare-storage \
    && chmod 644 /opt/ira/alembic.ini \
    && chmod -R a+rX /opt/ira/migrations \
    && install -d -o 10001 -g 10001 -m 0700 /tmp/ira-runtime

USER 10001:10001
CMD ["uvicorn", "intelligent_reimbursement_assitant.entrypoints.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
