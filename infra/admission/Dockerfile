FROM python:3.12.12-slim@sha256:f3fa41d74a768c2fce8016b98c191ae8c1bacd8f1152870a3f9f87d350920b7c
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONNOUSERSITE=1 PYTHONPATH=/app
COPY requirements.txt /tmp/admission-requirements.txt
RUN PIP_CONFIG_FILE=/dev/null pip install --disable-pip-version-check --no-cache-dir --require-hashes --only-binary=:all: --index-url https://pypi.org/simple -r /tmp/admission-requirements.txt
WORKDIR /app
COPY app /app/app
LABEL codespy.admission="source-zip-v1"
USER 65534:65534
ENTRYPOINT ["python", "-m", "app.admission.entrypoint"]
