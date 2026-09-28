# syntax=docker/dockerfile:1
# Slim image that runs the RepoRewind CLI (and the bundled offline demo).
# Both base images are pinned by digest, so a rebuild starts from the same bytes.
ARG PYTHON_IMAGE=python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc

FROM ${UV_IMAGE} AS uv

# Build stage: install the locked runtime dependencies and the package into a venv.
FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# Runtime stage: git (the CLI shells out to it, never through a shell) and the venv.
FROM ${PYTHON_IMAGE}
LABEL project=reporewind \
      org.opencontainers.image.title="reporewind" \
      org.opencontainers.image.source="https://github.com/vipul21435/reporewind" \
      org.opencontainers.image.licenses="MIT"
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 rewind
COPY --from=builder /app/.venv /app/.venv
COPY demo /app/demo
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    REPOREWIND_HOME=/home/rewind/.reporewind
USER rewind
WORKDIR /home/rewind
ENTRYPOINT ["reporewind"]
CMD ["--help"]
