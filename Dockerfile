# Loom Control Plane production image.
#
# Builder stages, then the runtime (docs/operations/deployment.md lists each). uvicorn
# binds via the container's PORT env var.

# Base images are pinned by digest; Renovate (renovate.json) keeps the pins current.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS py-builder
COPY --from=ghcr.io/astral-sh/uv:0.12.2@sha256:069a51314a7bb6031777a9273205fe1b0b19e914ef418207d1338b268df641dd /uv /bin/uv
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock README.md ./
COPY server ./server
COPY sdk ./sdk
# [lemma] (lemmagen3) ships in the image: it is the lexical leg's English
# lemmatizer and needs build-essential to compile, which is why it is an
# extra rather than a core dep (a host that only runs the `loom` CLI must
# `pip install -e .` without a compiler).
# [aws] (boto3) ships in the image for `loom export-analytics` and for
# LOOM_DOCUMENT_OBJECT_STORE=s3 (document originals in a bucket).
# [code] extras (tree-sitter, numpy) ship in the image: the code-plane
# workers and the async code.refresh path lazy-import them at runtime,
# so a plain "." install boots green and then crashes on first ingest.
# [local-embedder] (fastembed plus onnxruntime) ships in the image: the
# released stack embeds with it, and the runtime stage carries its default
# model's pinned files, so a stack started with no network still embeds.
# Dependencies come from uv.lock exactly (--locked: the build fails when the
# lock no longer matches pyproject.toml; hashes checked) into a venv the
# runtime stage copies at the same path.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
RUN uv sync --locked --no-editable --no-cache \
    --extra aws --extra code --extra lemma --extra local-embedder

# The default local embedding model, at its pinned revision and checked by sha256
# (server/embedding/local_models.py), in a stage keyed on the venv and server/ alone,
# so a frontend or docs change does not fetch it again.
FROM py-builder AS local-models
RUN LOOM_LOCAL_EMBEDDING_MODEL_DIR=/models /opt/venv/bin/python -m server.embedding.local_models fetch bge-small

FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS frontend-builder
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
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS go-builder
ARG TARGETARCH
ARG GO_VERSION=1.27.3
ARG GO_SHA256_AMD64=65efbc80fdc1fa2cd2631cad5b481777a68e8725f34e89f50fc6ac714e379b40
ARG GO_SHA256_ARM64=a06e99309dbcbdca1a8ccd2282c3ee146e55930886e26b4f1193269587165f49
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
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS node-builder
ARG TARGETARCH
ARG NODE_VERSION=22.23.4
ARG NODE_SHA256_X64=1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce
ARG NODE_SHA256_ARM64=6287aa46b6c375d816e63ae66d024fbbfd70404825aabdfdf898a970871d2f4b
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

# The C++ compiler tier (code_scip_delta, code_scip_full). scip-clang ships an
# x86_64 Linux binary only, so the image carries it on amd64; elsewhere point
# LOOM_CODE_SCIP_CLANG_PATH at your own build. The keyring holds the pinned LLVM
# signing key alone: the runtime stage installs clang, lld and the llvm tools a
# repository's CMake configure looks for (it writes the compile_commands.json
# scip-clang reads).
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS cpp-builder
ARG TARGETARCH
ARG SCIP_CLANG_VERSION=v0.4.0
ARG SCIP_CLANG_SHA256_AMD64=06fd18c576f979a726c651594644ec4a35db4f471f2160b3f72eb89fa6001784
ARG LLVM_SIGNER_FPR=6084F3CF814B57C1CF12EFD515CF4D18AF4F7421
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /out/bin \
    && export GNUPGHOME="$(mktemp -d)" \
    && curl -fsSL https://apt.llvm.org/llvm-snapshot.gpg.key | gpg --batch --import \
    && gpg --batch --export "${LLVM_SIGNER_FPR}" >/out/llvm.gpg \
    && [ "$(gpg --show-keys --with-colons /out/llvm.gpg | grep -c '^pub:')" = 1 ] \
    && gpg --show-keys --with-colons /out/llvm.gpg | grep -q "^fpr:::::::::${LLVM_SIGNER_FPR}:" \
    && if [ "${TARGETARCH:-amd64}" = amd64 ]; then \
         curl -fsSL "https://github.com/sourcegraph/scip-clang/releases/download/${SCIP_CLANG_VERSION}/scip-clang-x86_64-linux" -o /out/bin/scip-clang \
         && printf '%s  /out/bin/scip-clang\n' "${SCIP_CLANG_SHA256_AMD64}" | sha256sum -c \
         && chmod 0755 /out/bin/scip-clang; \
       fi

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
# git: the code_ingest worker and async code.refresh maintain the bare
# repo mirror (fetch + cat-file) by shelling out to git. Mounted repos are
# owned by the host operator, not uid 999 — without safe.directory git
# refuses every command with "dubious ownership".
# bubblewrap: the repository-job sandbox (clone, fetch, dependency installs and
# the compiler tiers run inside it). It needs user namespaces, which Docker's
# default seccomp profile blocks: see "Repository-job sandbox" in
# docs/operations/code-index-operations.md for the container options.
# clang 21, lld, the llvm tools, cmake and ninja: the C++ tier's CMake configure
# (plain http: apt checks the signature against the pinned key).
COPY --from=cpp-builder /out/llvm.gpg /usr/share/keyrings/llvm.gpg
RUN echo "deb [signed-by=/usr/share/keyrings/llvm.gpg] http://apt.llvm.org/trixie/ llvm-toolchain-trixie-21 main" \
      >/etc/apt/sources.list.d/llvm.list \
    && apt-get update && apt-get install -y --no-install-recommends \
    bubblewrap \
    ca-certificates \
    clang-21 \
    cmake \
    git \
    lld-21 \
    llvm-21 \
    ninja-build \
    xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system safe.directory '*'
COPY --from=cpp-builder /out/bin/ /usr/local/bin/
WORKDIR /app
# Deployed commit, stamped in at build time and surfaced in /v1/health. Placed
# after the dependency layers so re-stamping a new commit doesn't bust the
# pip/npm caches. Defaults to "" (source runs report build_commit="unknown").
ARG LOOM_BUILD_COMMIT=""
ENV LOOM_BUILD_COMMIT=${LOOM_BUILD_COMMIT}
COPY LICENSE NOTICE THIRD_PARTY_NOTICES ./
COPY --from=py-builder /opt/venv /opt/venv
# `go` + scip-go on PATH so code_scip_go_delta can run in the
# workers container (a repository's Go compiler tier refreshes per merge)
COPY --from=go-builder /usr/local/go /usr/local/go
COPY --from=go-builder /usr/local/bin/scip-go /usr/local/bin/scip-go
# node, scip-typescript and pnpm on PATH
# so code_scip_ts_delta can run in the workers container (a repository's
# TypeScript compiler tier refreshes per merge instead of by a host-side run)
COPY --from=node-builder /usr/local/node /usr/local/node
ENV PATH=/opt/venv/bin:/usr/local/node/bin:/usr/local/go/bin:$PATH GOFLAGS=-mod=mod GOMODCACHE=/app/var/gomodcache GOCACHE=/app/var/gocache
COPY server ./server
COPY sdk ./sdk
# Schema migrations: the `/v1/admin/schema.status` endpoint reads these
# at runtime to compute expected-vs-present table drift. Without them
# the endpoint flags every existing table as "extra".
COPY schemas ./schemas
# The assistant's agent definition, which a boot seeds into the LibreChat beside it.
COPY integrations/librechat/agent.yaml ./integrations/librechat/agent.yaml
# The compiler-tier workers (code_scip_delta, code_scip_go_delta,
# code_scip_ts_delta, code_scip_py_delta) shell out to scripts/scip_harness/run_*.sh resolved
# relative to server/ — without this COPY every in-container pass reports the
# harness missing, whatever toolchain the image carries.
COPY scripts/scip_harness ./scripts/scip_harness
COPY scripts/serve.sh ./scripts/serve.sh
# code.calibrate reads its probe corpus from bench/fixtures at a path
# hard-coded relative to the handler — without this COPY the op 422s
# in-container ("probe corpus missing").
COPY bench/fixtures ./bench/fixtures
# mentor.guide serves the agent guide (SKILL.md, reference.md) from here.
COPY skills/loom ./skills/loom
# help.search and help.get read the documentation from here (its Markdown; the
# screenshots under docs/images/ stay out of the build context, .dockerignore).
COPY docs ./docs
# Dev tooling — loom_init.py, loom_ingest.py, verify_audit_chain.py, backup
# and restore. Migrations run in the server's startup lifespan; these ship for
# `docker exec` operational tasks (token mint, knowledge ingest, audit verification).
COPY dev ./dev
# Built React/Vite dashboard. Served at / (index.html) and /assets/*.
# If this layer is missing, dashboard pages return 503 — admin endpoints
# at /v1/admin/* stay reachable via curl regardless.
COPY --from=frontend-builder /frontend/dist ./frontend/dist

# Non-root runtime. uid/gid 999: the compose files mount /run/loom (the
# per-server runtime files) as a tmpfs owned by it.
RUN groupadd -r -g 999 loom && useradd -r -u 999 -g loom -d /app loom \
    && mkdir -p /app/var/code_repos /app/var/gomodcache /app/var/gocache /app/var/cron/locks \
    && chown -R loom:loom /app \
    && install -d -o loom -g loom -m 0700 /run/loom
USER loom

# LOOM_BIND_HOST is the address the port is published on, which the startup
# checks read; LOOM_LISTEN_HOST is the one the container listens on, all its
# interfaces. 0.0.0.0 = reachable by others, so it needs the safeguards in
# docs/operations/deployment.md ("Proxy and TLS"). A compose stack that
# publishes on loopback passes its LOOM_BIND_HOST through.
# PYTHONPATH: the `loom` command imports the same /app tree as the server,
# not the copy in the venv, which ships without schemas/.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PORT=7777 \
    LOOM_BIND_HOST=0.0.0.0 \
    LOOM_LISTEN_HOST=0.0.0.0 \
    LOOM_LOCAL_EMBEDDING_MODEL_DIR=/app/models

# Outside /app/var, which a deployment may mount over (the Helm chart's cache volume).
COPY --from=local-models --chown=loom:loom /models /app/models

EXPOSE 7777
# Readiness, not liveness: healthy once the replica can serve (migrations, keys
# and warm-up loaded), unhealthy while it drains. The start period covers a
# boot that waits on another replica's migration lease.
HEALTHCHECK --interval=10s --timeout=5s --start-period=900s --retries=3 \
    CMD python -c "import urllib.request, sys; r=urllib.request.urlopen('http://127.0.0.1:'+__import__('os').environ.get('PORT','7777')+'/v1/health/ready', timeout=3); sys.exit(0 if r.status==200 else 1)" || exit 1

# The web tier: scripts/serve.sh says how it starts and stops. On SIGTERM Loom
# answers not-ready for LOOM_SHUTDOWN_READY_DELAY_S (15 s) while it serves, uvicorn
# then waits at most LOOM_GRACEFUL_SHUTDOWN_S (10 s) for in-flight requests, the
# lifespan drains take at most 40 s (server/boot.py SHUTDOWN_DRAIN_BUDGET_S), and
# participant retirement runs last: compose's stop_grace_period is 75 s.
CMD ["/app/scripts/serve.sh"]
