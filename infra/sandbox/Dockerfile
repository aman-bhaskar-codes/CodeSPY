# The image every sandbox command runs in. It holds tools only: no source, no credentials and no
# entrypoint, and it is started read-only, without network or capabilities (see adapters/sandbox/docker.py).
FROM python:3.12.12-slim@sha256:f3fa41d74a768c2fce8016b98c191ae8c1bacd8f1152870a3f9f87d350920b7c
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONNOUSERSITE=1 LANG=C.UTF-8
WORKDIR /workspace
LABEL codespy.sandbox.image="python-v1"
