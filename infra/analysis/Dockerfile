FROM node:22.23.3-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS node
RUN cp -L /usr/lib/*-linux-gnu/libstdc++.so.6 /tmp/libstdc++.so.6
FROM python:3.12.12-slim@sha256:f3fa41d74a768c2fce8016b98c191ae8c1bacd8f1152870a3f9f87d350920b7c
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /tmp/libstdc++.so.6 /usr/local/lib/libstdc++.so.6
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ldconfig
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONNOUSERSITE=1 PYTHONPATH=/opt/codespy/backend
WORKDIR /opt/codespy
COPY requirements.txt ./requirements.txt
RUN PIP_CONFIG_FILE=/dev/null pip install --disable-pip-version-check --no-cache-dir --require-hashes --only-binary=:all: --index-url https://pypi.org/simple -r requirements.txt
COPY tools/typescript-analyzer ./tools/typescript-analyzer
RUN cd tools/typescript-analyzer && node /usr/local/lib/node_modules/npm/bin/npm-cli.js ci --ignore-scripts && node /usr/local/lib/node_modules/npm/bin/npm-cli.js run build && rm -rf /root/.npm
COPY backend ./backend
COPY toolchain.sha256 ./toolchain.sha256
LABEL codespy.analysis="structural-small-v1"
USER 65534:65534
ENTRYPOINT ["python", "-m", "app.analyzers.entrypoint"]
