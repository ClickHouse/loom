FROM python:3.12-slim

WORKDIR /opt/loom-source-worker
COPY pyproject.toml ./
COPY loom_source_worker/ ./loom_source_worker/
RUN pip install --no-cache-dir .

USER 10001:10001
ENTRYPOINT ["python", "-IBu", "-m", "loom_source_worker.main"]
