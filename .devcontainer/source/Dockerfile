FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
COPY --from=ghcr.io/astral-sh/uv:0.12.2@sha256:069a51314a7bb6031777a9273205fe1b0b19e914ef418207d1338b268df641dd /uv /usr/local/bin/uv

ARG CONTRIBUTOR_UID=1000
RUN apt-get update && apt-get install -y --no-install-recommends build-essential ca-certificates git rustfmt \
    && useradd --create-home --uid "$CONTRIBUTOR_UID" developer \
    && mkdir /venv && chown developer:developer /venv

ENV UV_PROJECT_ENVIRONMENT=/venv \
    UV_CACHE_DIR=/tmp/loom-uv-cache \
    PYTHONDONTWRITEBYTECODE=1 \
    LOOM_ENV_FILE="" \
    LOOM_LLM_PROVIDER=fake \
    LOOM_EMBEDDING_MODEL=fake-ngram-1024 \
    LOOM_BACKGROUND_WORKERS=off
USER developer
WORKDIR /workspaces/loom-source
