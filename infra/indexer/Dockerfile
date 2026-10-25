FROM python:3.12.12-slim@sha256:f3fa41d74a768c2fce8016b98c191ae8c1bacd8f1152870a3f9f87d350920b7c
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONNOUSERSITE=1 PYTHONPATH=/app TIKTOKEN_CACHE_DIR=/opt/tokenizer
COPY requirements.txt /tmp/index-requirements.txt
RUN PIP_CONFIG_FILE=/dev/null pip install --timeout=60 --retries=2 --disable-pip-version-check --no-cache-dir --require-hashes --only-binary=:all: --index-url https://pypi.org/simple -r /tmp/index-requirements.txt
WORKDIR /app
COPY app /app/app
COPY runtime.json /app/runtime.json
COPY vocabulary /opt/tokenizer/9b5ad71b2ce5302211f9c61530b329a4922fc6a4
LABEL codespy.indexer="safe-windows-v1"
USER 65534:65534
ENTRYPOINT ["python", "-m", "app.indexing.entrypoint"]
