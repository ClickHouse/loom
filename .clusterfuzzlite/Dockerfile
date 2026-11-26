FROM gcr.io/oss-fuzz-base/base-builder-python@sha256:21f6fab17ace4dc76e1c56de91ee95d3d03da021df94b41056ba036f6c6f55e9

COPY .clusterfuzzlite/requirements.txt /src/loom-fuzz-requirements.txt
RUN python3 -m pip install --no-cache-dir --only-binary=:all: --require-hashes --no-deps -r /src/loom-fuzz-requirements.txt

COPY server/__init__.py /src/loom/server/__init__.py
COPY server/core/__init__.py server/core/embedding_space.py /src/loom/server/core/
COPY tests/__init__.py /src/loom/tests/__init__.py
COPY tests/fuzz/ /src/loom/tests/fuzz/
COPY .clusterfuzzlite/build.sh /src/build.sh
WORKDIR /src/loom
