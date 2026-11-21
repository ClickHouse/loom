ARG LOCAL_MODELS_IMAGE
FROM ${LOCAL_MODELS_IMAGE} AS dependencies
FROM python:3.12-bookworm@sha256:4209fae09a3e20afd863e528bfddc235da5fd5977983e1996cd54cc9a72e6267
RUN git --version && /usr/local/bin/python -c "import ctypes; ctypes.CDLL('libgomp.so.1')" \
    && git config --system safe.directory '*'
COPY --from=dependencies /opt/venv /opt/venv
COPY --from=dependencies /models /models
WORKDIR /app
COPY server ./server
COPY sdk ./sdk
COPY schemas ./schemas
COPY examples/software_factory/factory/model.py ./examples/software_factory/factory/model.py
COPY examples/software_factory/harbor/native_setup.py ./examples/software_factory/harbor/native_setup.py
COPY examples/software_factory/harbor/index_setup.py ./examples/software_factory/harbor/index_setup.py
COPY examples/software_factory/harbor/source_tree.py ./examples/software_factory/harbor/source_tree.py
COPY scripts/scip_harness ./scripts/scip_harness
COPY LICENSE NOTICE THIRD_PARTY_NOTICES ./
RUN groupadd -r -g 999 loom && useradd -r -u 999 -g loom -d /app loom \
    && mkdir -p /app/var /run/loom && chown -R loom:loom /app/var /run/loom
ENV PATH=/opt/venv/bin:$PATH PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1 \
    LOOM_ENV_FILE= LOOM_LOCAL_EMBEDDING_MODEL_DIR=/models
USER loom
CMD ["/opt/venv/bin/uvicorn","server.main:app","--host","0.0.0.0","--port","7777","--loop","asyncio"]
