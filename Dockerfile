# Pinned base: same image in both stages, digest keeps the build reproducible.
ARG PYTHON_IMAGE=python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# --- build stage: resolve and download the wheels once ---
FROM ${PYTHON_IMAGE} AS build

COPY requirements.txt /tmp/
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r /tmp/requirements.txt

# --- runtime stage: only the installed packages and the service ---
FROM ${PYTHON_IMAGE}

LABEL org.opencontainers.image.title="vps-incident-assistant"
LABEL org.opencontainers.image.source="https://github.com/PhilippKlinger/vps-incident-assistant"

# Fixed UID, so file permissions and the CI check do not depend on the base image.
RUN groupadd --system --gid 10001 app && \
    useradd --system --uid 10001 --gid app --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Dependencies before the source code, so the layer stays cached.
# The bind mount keeps the wheel files themselves out of the image layers.
RUN --mount=type=bind,from=build,source=/wheels,target=/wheels \
    pip install --no-cache-dir --no-index /wheels/*.whl

# Owned by root and only readable for the app user: the service cannot change its own code.
# config.toml is read from the parent of the package directory (/app/config.toml).
WORKDIR /app
COPY config.toml ./
COPY assistant ./assistant

USER 10001:10001

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"]

CMD ["python", "-m", "assistant"]
