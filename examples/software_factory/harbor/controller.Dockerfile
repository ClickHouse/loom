# The launcher prepends the production py-builder stage with locked SCIP and
# extractor extras. No frontend or other compiler toolchain stages are built.
FROM docker:29.1.3-cli@sha256:4fa0ee1f3a7e4354c4ea34558b6d4ee32859baf4973d4c8ccc8e7fe3dd730c04 AS factory-docker
FROM python:3.12-bookworm@sha256:4209fae09a3e20afd863e528bfddc235da5fd5977983e1996cd54cc9a72e6267 AS factory-controller
RUN git --version && gcc --version
COPY --from=py-builder /opt/venv /opt/venv
COPY --from=factory-docker /usr/local/bin/docker /usr/local/bin/docker
COPY --from=factory-docker /usr/local/libexec/docker/cli-plugins/ /usr/local/libexec/docker/cli-plugins/
RUN python -m venv /opt/controller
COPY controller-requirements.txt /opt/controller-requirements.txt
RUN /opt/controller/bin/python -m pip install --no-cache-dir --require-hashes \
    -r /opt/controller-requirements.txt
RUN /opt/controller/bin/python -c \
    "from importlib.metadata import version; assert version('harbor') == '0.27.0'" \
    && /opt/venv/bin/python -c \
    "import pyarrow,google.protobuf; assert tuple(map(int,google.protobuf.__version__.split('.'))) >= (7,35,1)" \
    && docker --version && docker compose version && docker buildx version
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    FACTORY_LOOM_PYTHON=/opt/venv/bin/python
ENTRYPOINT ["/opt/controller/bin/python", "-m", "examples.software_factory.harbor.one_shot"]
