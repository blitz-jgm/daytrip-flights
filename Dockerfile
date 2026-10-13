# syntax=docker/dockerfile:1
FROM python:3.12.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /usr/local/bin/uv

# supercronic: cron for containers (single static binary, logs to stdout, no root daemon).
ARG TARGETARCH
ARG SUPERCRONIC_VERSION=v0.2.49
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl tzdata; \
    rm -rf /var/lib/apt/lists/*; \
    case "${TARGETARCH}" in \
      amd64) sha=a53ae236602c7338aba3fbaff40bda6300eae3b9fedb8261eb06cfe3724430c1 ;; \
      arm64) sha=02aa0cb229ba09050cba6638059dadb9eedc2276632ea43d6a57a2f8c1629dd5 ;; \
      *) echo "unsupported architecture: ${TARGETARCH}"; exit 1 ;; \
    esac; \
    curl -fsSLo /usr/local/bin/supercronic \
      "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${TARGETARCH}"; \
    echo "${sha}  /usr/local/bin/supercronic" | sha256sum -c -; \
    chmod +x /usr/local/bin/supercronic

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# dependencies first for layer caching, then the project itself
COPY pyproject.toml uv.lock README.md .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

COPY config.yaml crontab ./
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

ENV PATH=/opt/venv/bin:$PATH \
    TZ=Asia/Seoul \
    DAYTRIP_DATA_DIR=/data \
    DAYTRIP_CONFIG=/app/config.yaml

RUN useradd --uid 1000 --create-home app && mkdir /data && chown app /data
USER app
VOLUME ["/data"]

ENTRYPOINT ["entrypoint.sh"]
CMD ["schedule"]
