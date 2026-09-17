# Chronicle images: api, builder, preview, one stage each, from this single
# Dockerfile with the repository root as build context (spec section 14).
#
#   docker build --target api .
#   docker build --target builder .
#   docker build --target preview .
#
# All three run non-root (uid 1000) and are meant to run with a read-only
# root filesystem. api expects a writable volume at CHRONICLE_DATA_DIR;
# builder expects the same plus a writable Hugo cache under its home
# directory; preview expects a writable volume at CHRONICLE_PREVIEW_DIR. See
# README.md for the full volume list.
#
# Every CMD below uses exec form, so nothing here invokes a shell at
# runtime. The base image still carries /bin/sh from Debian itself; dropping
# it would mean a distroless base, which is future work, not this round's.

ARG HUGO_VERSION=0.164.0

FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS base

ARG BUILD_VERSION=dev
ARG BUILD_SHA=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.source="https://github.com/sentania-labs/chronicle" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${BUILD_VERSION}" \
      org.opencontainers.image.revision="${BUILD_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}"

# The base image is pinned by digest so a rebuild is reproducible, which also
# freezes its distribution packages as they were the day that image was
# published. Applying the distribution's security updates here is what keeps
# the image scan clean between base image rebuilds. git is installed here,
# not just in the builder stage, because the api container performs its own
# git operations against the internal-only history (spec section 14).
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.10.12@sha256:72ab0aeb448090480ccabb99fb5f52b0dc3c71923bffb5e2e26517a1c27b7fec /uv /usr/local/bin/uv

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

COPY pyproject.toml uv.lock README.md ./
COPY chronicle ./chronicle
# uv and pip are build tools, removed from every runtime image below: nothing
# in a running container installs anything, and their own bundled
# dependencies are the usual source of image scan findings that no bump here
# can fix.
RUN uv sync --frozen --no-dev \
    && rm -f /usr/local/bin/uv \
    && rm -rf /usr/local/lib/python3.12/site-packages/pip* /usr/local/bin/pip*

ENV CHRONICLE_BUILD_VERSION="${BUILD_VERSION}"

# /data, /data/preview, and /data/builder-work are created and handed to
# uid 1000 here so a fresh Docker named volume (compose) inherits this
# ownership on first mount instead of the root:root default Docker would
# otherwise create. That default is what a `docker compose down -v` /
# `up -d` cycle exposed: /data/preview is its own named volume (mounted
# separately from /data in every container that touches it), so its
# ownership at first mount comes from whatever exists at that path in the
# image, not from /data's, and a directory that does not exist in the
# image at all mounts in as root:root. /data/builder-work does not need
# its own volume (it lives under /data), but is created here too so a
# builder that starts before ever calling `mkdir` still finds the right
# owner on a from-empty volume. A Kubernetes PVC gets the same result from
# the pod's fsGroup instead (see examples/k8s/deployment.yaml).
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin chronicle \
    && mkdir -p /data /data/preview /data/builder-work \
    && chown -R chronicle:chronicle /data

# -----------------------------------------------------------------------------
FROM base AS api

LABEL org.opencontainers.image.title="chronicle-api" \
      org.opencontainers.image.description="Chronicle API: the public contract, UI backend, admin, GitHub client, reconciler"

USER 1000
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"]

CMD ["uvicorn", "chronicle.api.main:app", "--host", "0.0.0.0", "--port", "8080"]

# -----------------------------------------------------------------------------
FROM base AS builder

ARG HUGO_VERSION
ARG TARGETARCH

LABEL org.opencontainers.image.title="chronicle-builder" \
      org.opencontainers.image.description="Chronicle preview builder: Hugo extended, blog repo clone, run queue watcher"

# git (installed in the base stage) clones the blog repo at main with
# submodules (spec section 8). Hugo extended is installed from the pinned
# GitHub release .deb rather than Debian's package so its version tracks
# HUGO_VERSION exactly; rebuilding this image with a new HUGO_VERSION is how
# a Hugo version bump is followed. TARGETARCH is set by BuildKit to the
# Docker platform architecture (amd64, arm64) and picks the matching release
# artifact; any other value fails the build instead of silently fetching the
# wrong one.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && case "${TARGETARCH}" in \
         amd64) hugo_arch="amd64" ;; \
         arm64) hugo_arch="arm64" ;; \
         *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
       esac \
    && curl -fsSL -o /tmp/hugo.deb \
       "https://github.com/gohugoio/hugo/releases/download/v${HUGO_VERSION}/hugo_extended_${HUGO_VERSION}_linux-${hugo_arch}.deb" \
    && apt-get install -y --no-install-recommends /tmp/hugo.deb \
    && rm -f /tmp/hugo.deb \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/*

USER 1000

# The builder has no HTTP port to probe; liveness is the heartbeat file it
# writes every poll tick (chronicle/builder/runner.py). A heartbeat older
# than five poll intervals means the loop has stopped making progress, not
# necessarily crashed, which is exactly what a liveness probe should catch
# either way.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python3", "-m", "chronicle.builder.main", "--healthcheck"]

CMD ["python3", "-m", "chronicle.builder.main"]

# -----------------------------------------------------------------------------
FROM base AS preview

LABEL org.opencontainers.image.title="chronicle-preview" \
      org.opencontainers.image.description="Chronicle preview: static file server over the preview volume, directory listing disabled"

USER 1000
EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz')"]

CMD ["python3", "-m", "chronicle.preview.main"]
