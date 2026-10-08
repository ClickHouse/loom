# Loom Control Plane production image.
#
# Three stages: Python deps, frontend bundle, runtime. uvicorn binds via
# the container's PORT env var.

FROM python:3.12-slim AS py-builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY server ./server
COPY sdk ./sdk
# [lemma] (lemmagen3) ships in the image: it is the lexical leg's English
# lemmatizer and needs build-essential to compile, which is why it is an
# extra rather than a core dep (a host that only runs the `loom` CLI must
# `pip install -e .` without a compiler).
# [code] extras (tree-sitter, numpy) ship in the image: the code-plane
# workers and the async code.refresh path lazy-import them at runtime,
# so a plain "." install boots green and then crashes on first ingest.
#
# [local-embedder] (fastembed plus onnxruntime, about 80-100MB) ships only
# when LOOM_EMBEDDING_MODEL is "local*" at build time. It arrives as a
# build arg, so operators on a cloud embedder never pay for a dependency
# they don't import. Flipping LOOM_EMBEDDING_MODEL between local and cloud
# in .env re-triggers this layer on the next --build.
ARG LOOM_EMBEDDING_MODEL=""
RUN pip install --no-cache-dir --upgrade pip \
    && case "$LOOM_EMBEDDING_MODEL" in \
         local*) EXTRAS=".[code,lemma,local-embedder]" ;; \
         *) EXTRAS=".[code,lemma]" ;; \
       esac \
    && pip install --no-cache-dir --prefix=/install "$EXTRAS"

FROM node:22-alpine AS frontend-builder
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
COPY scripts/release/frontend_third_party_notices.mjs /tmp/loom-license/
COPY scripts/release/license_fallbacks /tmp/loom-license/license_fallbacks
RUN node /tmp/loom-license/frontend_third_party_notices.mjs --root /frontend \
    --output /frontend/THIRD_PARTY_NOTICES.txt \
    && npm run build \
    && cp /frontend/THIRD_PARTY_NOTICES.txt /frontend/dist/THIRD_PARTY_NOTICES.txt

# The Go toolchain and scip-go for the Go compiler-tier worker
# (code_scip_go_delta), built here so the runtime stage copies a finished
# /usr/local/go and one binary. Pinned; TARGETARCH picks the tarball (amd64 or
# arm64), checked against the sha256 go.dev publishes for it (a new GO_VERSION
# needs its two checksums too). scip-go's module download is verified by the Go
# checksum database.
FROM python:3.12-slim AS go-builder
ARG TARGETARCH
ARG GO_VERSION=1.23.4
ARG GO_SHA256_AMD64=6924efde5de86fe277676e929dc9917d466efa02fb934197bc2eba35d5680971
ARG GO_SHA256_ARM64=16e5017863a7f6071363782b1b8042eb12c6ca4f4cd71528b2123f0a1275b13e
ARG SCIP_GO_VERSION=v0.2.7
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/* \
    && case "${TARGETARCH:-amd64}" in \
         amd64) GO_SHA256="${GO_SHA256_AMD64}" ;; \
         arm64) GO_SHA256="${GO_SHA256_ARM64}" ;; \
         *) echo "no pinned Go tarball for ${TARGETARCH}" >&2; exit 1 ;; \
       esac \
    && curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-${TARGETARCH:-amd64}.tar.gz" -o /tmp/go.tgz \
    && printf '%s  /tmp/go.tgz\n' "${GO_SHA256}" >/tmp/go.tgz.sha256 \
    && sha256sum -c /tmp/go.tgz.sha256 \
    && tar -C /usr/local -xzf /tmp/go.tgz && rm /tmp/go.tgz /tmp/go.tgz.sha256
ENV PATH=/usr/local/go/bin:$PATH GOPATH=/go GOFLAGS=-mod=mod
RUN go install "github.com/scip-code/scip-go/cmd/scip-go@${SCIP_GO_VERSION}" \
    && cp /go/bin/scip-go /usr/local/bin/scip-go

# Node and scip-typescript for the
# TypeScript compiler-tier worker (code_scip_ts_delta), fetched the way the Go
# stage fetches Go — the official glibc tarball, pinned, TARGETARCH picks it —
# with compiler indexers and package managers installed into one prefix so the
# runtime stage can prepare fresh checkouts automatically. The tarball is
# checked against the sha256 in the release's
# SHASUMS256.txt (a new NODE_VERSION needs its two checksums too).
FROM python:3.12-slim AS node-builder
ARG TARGETARCH
ARG NODE_VERSION=22.23.2
ARG NODE_SHA256_X64=d60acfe00a2932254bb0ad20e01b0d74397a0875595de719654b214f4b03f307
ARG NODE_SHA256_ARM64=fff4078c5def658577f92c88db7db3bc0072924bfb93fe52c1e744a54e94abb8
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && case "${TARGETARCH:-amd64}" in \
         amd64) NODE_ARCH=x64 NODE_SHA256="${NODE_SHA256_X64}" ;; \
         arm64) NODE_ARCH=arm64 NODE_SHA256="${NODE_SHA256_ARM64}" ;; \
         *) echo "no pinned Node tarball for ${TARGETARCH}" >&2; exit 1 ;; \
       esac \
    && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz" -o /tmp/node.txz \
    && printf '%s  /tmp/node.txz\n' "${NODE_SHA256}" >/tmp/node.txz.sha256 \
    && sha256sum -c /tmp/node.txz.sha256 \
    && mkdir -p /usr/local/node && tar -C /usr/local/node --strip-components=1 -xJf /tmp/node.txz && rm /tmp/node.txz /tmp/node.txz.sha256
ENV PATH=/usr/local/node/bin:$PATH
COPY scripts/install-code-toolchains.sh /tmp/install-code-toolchains.sh
RUN LOOM_CODE_TOOLCHAIN_PREFIX=/usr/local/node sh /tmp/install-code-toolchains.sh \
    && npm cache clean --force

FROM python:3.12-slim
# git: the code_ingest worker and async code.refresh maintain the bare
# repo mirror (fetch + cat-file) by shelling out to git. Mounted repos are
# owned by the host operator, not uid 999 — without safe.directory git
# refuses every command with "dubious ownership".
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system safe.directory '*'
WORKDIR /app
# Deployed commit, stamped in at build time and surfaced in /v1/health. Placed
# after the dependency layers so re-stamping a new commit doesn't bust the
# pip/npm caches. Defaults to "" (source runs report build_commit="unknown").
ARG LOOM_BUILD_COMMIT=""
ENV LOOM_BUILD_COMMIT=${LOOM_BUILD_COMMIT}
COPY LICENSE NOTICE THIRD_PARTY_NOTICES ./
COPY --from=py-builder /install /usr/local
# `go` + scip-go on PATH so code_scip_go_delta can run in the
# workers container (a repository's Go compiler tier refreshes per merge)
COPY --from=go-builder /usr/local/go /usr/local/go
COPY --from=go-builder /usr/local/bin/scip-go /usr/local/bin/scip-go
# node, scip-typescript and pnpm on PATH
# so code_scip_ts_delta can run in the workers container (a repository's
# TypeScript compiler tier refreshes per merge instead of by a host-side run)
COPY --from=node-builder /usr/local/node /usr/local/node
ENV PATH=/usr/local/node/bin:/usr/local/go/bin:$PATH GOFLAGS=-mod=mod GOMODCACHE=/app/var/gomodcache GOCACHE=/app/var/gocache
COPY server ./server
COPY sdk ./sdk
# Schema migrations: the `/v1/admin/schema.status` endpoint reads these
# at runtime to compute expected-vs-present table drift. Without them
# the endpoint flags every existing table as "extra".
COPY schemas ./schemas
# The compiler-tier workers (code_scip_delta, code_scip_go_delta,
# code_scip_ts_delta, code_scip_py_delta) shell out to scripts/scip_harness/run_*.sh resolved
# relative to server/ — without this COPY every in-container pass reports the
# harness missing, whatever toolchain the image carries.
COPY scripts/scip_harness ./scripts/scip_harness
# code.calibrate reads its probe corpus from bench/fixtures at a path
# hard-coded relative to the handler — without this COPY the op 422s
# in-container ("probe corpus missing").
COPY bench/fixtures ./bench/fixtures
# Dev tooling — loom_init.py, loom_ingest.py, verify_audit_chain.py, backup
# and restore. Migrations run in the server's startup lifespan; these ship for
# `docker exec` operational tasks (token mint, knowledge ingest, audit verification).
COPY dev ./dev
# Built React/Vite dashboard. Served at /me (index.html) and /me/assets/*.
# If this layer is missing, the /me route returns 503 — admin endpoints
# at /v1/admin/* stay reachable via curl regardless.
COPY --from=frontend-builder /frontend/dist ./frontend/dist

# Non-root runtime
RUN groupadd -r loom && useradd -r -g loom -d /app loom \
    && mkdir -p /app/var/code_repos /app/var/blobs /app/var/gomodcache /app/var/gocache /app/var/cron/locks \
    && chown -R loom:loom /app
USER loom

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=7777

EXPOSE 7777
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request, sys; r=urllib.request.urlopen('http://127.0.0.1:'+__import__('os').environ.get('PORT','7777')+'/v1/health', timeout=3); sys.exit(0 if r.status==200 else 1)" || exit 1

# LOOM_WEB_WORKERS processes share the port; one of them
# takes the supervisor lock and runs the background workers.
# LOOM_UVICORN_LOOP: uvicorn's --loop (auto|asyncio|uvloop). `auto` picks uvloop
# when installed; `asyncio` is the switch back to CPython's event loop and SSL
# transport without rebuilding the image.
# LOOM_GRACEFUL_SHUTDOWN_S: uvicorn's --timeout-graceful-shutdown. On SIGTERM
# uvicorn stops listening at once and waits for in-flight requests; without a
# bound one slow request (a long memory.ask) holds the old
# container until docker's stop grace expires and SIGKILLs it, skipping the
# lifespan drains (events emitter, drop ledger, events stream). 10 s bounds
# the wait; compose's stop_grace_period leaves room for the drains after it.
# `exec`: the shell that expands these variables must hand PID 1 to uvicorn.
# Without it `sh` is PID 1 and a POSIX shell does not forward SIGTERM to its
# foreground child, so uvicorn never sees the stop, the graceful timeout
# never starts, and docker SIGKILLs the tree at stop_grace_period (exit 137,
# with tens of seconds of refused connections during a colour swap).
CMD ["sh", "-c", "exec uvicorn server.main:app --host 0.0.0.0 --port ${PORT} --workers ${LOOM_WEB_WORKERS:-1} --loop ${LOOM_UVICORN_LOOP:-auto} --timeout-graceful-shutdown ${LOOM_GRACEFUL_SHUTDOWN_S:-10} --proxy-headers --forwarded-allow-ips='*'"]
