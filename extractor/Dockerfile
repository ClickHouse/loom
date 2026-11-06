# Loom's reference extractor: the document parsers, as a service of their own.
#
# Built from the repository root (`docker build -f extractor/Dockerfile .`).
# The service runs as an unprivileged user that owns nothing in the image: the
# venv on every child's sys.path is root's, so an extraction a hostile source
# has taken over cannot change what the next one imports. Beyond the base image,
# only the Tesseract and Leptonica libraries and Tesseract's English model, for
# OCR a child runs in its own process through ctypes: no program, as a child may
# start no process. A build with OCR=off leaves them out (about 44 MB).

# Base images are pinned by digest; Renovate (renovate.json) keeps the pins current.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.2@sha256:069a51314a7bb6031777a9273205fe1b0b19e914ef418207d1338b268df641dd /uv /bin/uv
WORKDIR /build
# Dependencies come from extractor/uv.lock exactly (--locked: the build fails
# when the lock no longer matches pyproject.toml; hashes checked).
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY extractor/pyproject.toml extractor/uv.lock extractor/README.md ./
COPY extractor/loom_extractor ./loom_extractor
RUN uv sync --locked --no-editable --no-cache

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
ARG OCR=on
RUN if [ "$OCR" = on ]; then \
        apt-get update && apt-get install -y --no-install-recommends libtesseract5 tesseract-ocr-eng \
        && rm -rf /var/lib/apt/lists/*; \
    fi
COPY LICENSE NOTICE THIRD_PARTY_NOTICES /usr/share/doc/loom-extractor/
COPY --from=builder /opt/venv /opt/venv
# uid/gid 10001 owns no file in the image and no home directory.
RUN groupadd -r -g 10001 extractor && useradd -r -u 10001 -g extractor -d / -s /usr/sbin/nologin extractor
USER 10001:10001

# The service listens on every interface of its container; the compose file and
# the chart keep that network to the Loom server alone.
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOOM_EXTRACTOR_HOST=0.0.0.0 \
    LOOM_EXTRACTOR_PORT=8790

EXPOSE 8790
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8790/healthz', timeout=2).status == 200 else 1)"]

CMD ["loom-extractor"]
