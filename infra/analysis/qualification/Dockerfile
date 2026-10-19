FROM node:22.23.3-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS node
RUN cp -L /usr/lib/*-linux-gnu/libstdc++.so.6 /tmp/libstdc++.so.6
FROM python:3.12.12-slim@sha256:f3fa41d74a768c2fce8016b98c191ae8c1bacd8f1152870a3f9f87d350920b7c
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /tmp/libstdc++.so.6 /usr/local/lib/libstdc++.so.6
COPY --from=node /usr/local/LICENSE /usr/local/share/licenses/node/LICENSE
COPY --from=node /usr/share/doc/libstdc++6/copyright /usr/local/share/licenses/libstdc++6/copyright
RUN ldconfig
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONNOUSERSITE=1
COPY requirements.txt /probe/requirements.txt
RUN PIP_CONFIG_FILE=/dev/null pip install --disable-pip-version-check --no-cache-dir --require-hashes --only-binary=:all: --index-url https://pypi.org/simple -r /probe/requirements.txt
COPY typescript /typescript
WORKDIR /typescript
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN node /usr/local/lib/node_modules/npm/bin/npm-cli.js ci --ignore-scripts --omit=dev && rm -rf /root/.npm
COPY probe.py progress_probe.py /probe/
LABEL codespy.phase3.qualification="structural-toolchain-probe-v1"
USER 65534:65534
WORKDIR /probe
ENTRYPOINT ["python", "/probe/probe.py"]
