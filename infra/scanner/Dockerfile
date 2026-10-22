FROM node:22.23.3-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS libraries
RUN cp -L /usr/lib/*-linux-gnu/libstdc++.so.6 /tmp/libstdc++.so.6
FROM python:3.12.12-slim@sha256:f3fa41d74a768c2fce8016b98c191ae8c1bacd8f1152870a3f9f87d350920b7c
COPY --from=libraries /tmp/libstdc++.so.6 /usr/local/lib/libstdc++.so.6
RUN ldconfig
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONNOUSERSITE=1 PYTHONPATH=/opt/codespy/backend
WORKDIR /opt/codespy
COPY infra/scanner/requirements.txt /opt/codespy/scanner-requirements.txt
RUN PIP_CONFIG_FILE=/dev/null pip install --disable-pip-version-check --no-cache-dir --require-hashes --only-binary=:all: --index-url https://pypi.org/simple -r scanner-requirements.txt
COPY infra/scanner/install_gitleaks.py /opt/codespy/install_gitleaks.py
RUN python install_gitleaks.py && rm install_gitleaks.py
COPY infra/scanner/rules /opt/codespy/rules
COPY backend/app /opt/codespy/backend/app
COPY infra/scanner/build_manifest.py infra/scanner/runtime-layout.json /opt/codespy/
RUN python build_manifest.py runtime-layout.json runtime.json && rm build_manifest.py
LABEL codespy.scanner="offline-source-v1"
USER 65534:65534
ENTRYPOINT ["python", "-m", "app.scanners.entrypoint", "--runtime", "/opt/codespy/runtime.json"]
