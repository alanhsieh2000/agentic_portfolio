# syntax=docker/dockerfile:1
#
# The syntax directive above is required, not decorative: this file uses
# `COPY --chmod=` and `RUN --mount=`, both of which need the BuildKit Dockerfile
# frontend rather than the legacy builder.
#
# Release image for agentic-portfolio. The DEVELOPMENT container is a separate
# file, docker/Dockerfile.dev - see docker/README.md for why they are not shared.
#
# Build (from the repository root, which must contain data/portfolio.duckdb):
#     SHA=$(cut -d' ' -f1 docker/dataset.sha256)
#     docker build --build-arg DATASET_SHA256="$SHA" -t agentic-portfolio .
#
# Run:
#     docker run --rm -it --user "$(id -u):$(id -g)" -v "$PWD:/work" -w /work \
#         -e ANTHROPIC_API_KEY agentic-portfolio portfolio-backtest
#
# Base choice. python:3.12-slim-bookworm, pinned to a patch release so a rebuild
# is reproducible. Alpine is not an option: its musl C library means the
# prebuilt manylinux wheels for numpy, scipy, cvxpy, duckdb and curl_cffi do not
# apply, so those would compile from source - under emulation, for the arm64
# build. A distroless base is not an option either, because `IMAGE bash` is part
# of the interface this image promises.

# Pinned rather than :latest, so the build tool cannot change under us.
FROM ghcr.io/astral-sh/uv:0.9.2 AS uv


# ---------------------------------------------------------------------------
# deps: the multi-gigabyte dependency layer.
#
# Keyed on pyproject.toml and uv.lock ALONE, so editing source code does not
# rebuild it. --no-dev omits the pytest/cvxpy dependency group; --no-install-project
# omits this project, which the wheel stage installs later.
# ---------------------------------------------------------------------------
FROM python:3.12.11-slim-bookworm AS deps

COPY --from=uv /uv /usr/local/bin/uv

# Compilers live in this stage only. Anything that needs to build from source
# does it here, and the runtime stage below never carries a toolchain.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential python3-dev \
    && rm -rf /var/lib/apt/lists/*

ENV UV_PROJECT_ENVIRONMENT=/opt/agentic-portfolio/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /build
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project


# ---------------------------------------------------------------------------
# wheel: tiny, and the only stage that source changes invalidate.
# ---------------------------------------------------------------------------
FROM deps AS wheel

# README.md is required: pyproject.toml names it as the package readme, so the
# build fails without it.
COPY README.md LICENSE NOTICE ./
COPY src ./src
RUN uv build --wheel --out-dir /dist


# ---------------------------------------------------------------------------
# provenance: describe the dataset from the dataset, so the manifest cannot
# disagree with the file it documents.
# ---------------------------------------------------------------------------
FROM deps AS provenance

COPY data/portfolio.duckdb /tmp/portfolio.duckdb
COPY docker/dataset_manifest.py /tmp/dataset_manifest.py
RUN /opt/agentic-portfolio/venv/bin/python /tmp/dataset_manifest.py \
        /tmp/portfolio.duckdb /dist/DATASET.json


# ---------------------------------------------------------------------------
# runtime
# ---------------------------------------------------------------------------
FROM python:3.12.11-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="agentic-portfolio" \
      org.opencontainers.image.description="Agentic AI screening for portfolio investment, with bundled market data" \
      org.opencontainers.image.licenses="MIT"

# Runtime shared libraries only, no toolchain. libgomp1 is OpenMP, wanted by
# numpy/scipy solver backends; libatomic1 is carried over from the development
# image, where duckdb needed it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 libatomic1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /usr/local/bin/uv

# A fixed non-root uid, so the image is not host-specific. Users who want files
# in a bind mount to belong to them pass --user "$(id -u):$(id -g)", which
# overrides this; /home/app and /work are world-writable so that an arbitrary
# uid can still write a dependency cache and a report. Nothing in this project
# calls Path.home(), expanduser() or getpass.getuser(), so a uid absent from
# /etc/passwd is harmless.
RUN groupadd -g 10001 app \
    && useradd -u 10001 -g 10001 -m -d /home/app -s /bin/bash app \
    && chmod 0777 /home/app \
    && install -d -m 0777 /work

# Layer 1: the dependency tree. Large, and changes only when the lock file does.
COPY --from=deps /opt/agentic-portfolio/venv /opt/agentic-portfolio/venv

# Layer 2: the bundled market data, ~45 MB, changes almost never.
#
# Mode 0444 is load-bearing, not tidiness. The portfolio-build-* commands open
# this database WRITABLE. Pointed at a path inside the image they would copy
# 45 MB into the container's ephemeral layer, appear to succeed, and discard the
# result on exit - a silent no-op, the worst possible failure. Read-only plus a
# non-root user turns that into an immediate permission error. Rebuilding is
# still supported: point DB_PATH at the mounted workspace instead, e.g.
# -e DB_PATH=/work/data/portfolio.duckdb.
COPY --chmod=0444 data/portfolio.duckdb /opt/agentic-portfolio/data/portfolio.duckdb

# Fail the build if the wrong dataset was baked, rather than shipping quietly.
# The expected value is committed in docker/dataset.sha256.
ARG DATASET_SHA256
RUN test -n "${DATASET_SHA256}" \
        || (echo "DATASET_SHA256 build-arg is required; see docker/README.md" >&2; exit 1) \
    && echo "${DATASET_SHA256}  /opt/agentic-portfolio/data/portfolio.duckdb" | sha256sum -c -

COPY --from=provenance /dist/DATASET.json /opt/agentic-portfolio/data/DATASET.json

# Straight from the build context: the wheel stage puts these in /build, not
# /dist, and there is no reason to route them through another stage.
COPY LICENSE NOTICE /opt/agentic-portfolio/

# Layer 3: this project. Kilobytes, and the layer a code change rebuilds.
# --no-deps because the dependency tree is already present and resolved.
# --mount rather than COPY so the wheel itself never lands in a layer; it needs
# BuildKit, which buildx uses by default and modern `docker build` enables.
RUN --mount=from=wheel,source=/dist,target=/dist \
    uv pip install --python /opt/agentic-portfolio/venv/bin/python --no-deps /dist/*.whl

ENV PATH="/opt/agentic-portfolio/venv/bin:${PATH}" \
    HOME=/home/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CREWAI_TELEMETRY_OPT_OUT=true \
    OTEL_SDK_DISABLED=true \
    DB_PATH=/opt/agentic-portfolio/data/portfolio.duckdb

# AGENTIC_PORTFOLIO_HOME is deliberately NOT set. Left unset it defaults to ".",
# so memory/, output/ and data/holdings.duckdb all resolve under the working
# directory - i.e. the user's mount - with no extra flag. DB_PATH is baked above
# precisely because the market-data database is the one path that should not
# follow the working directory.

USER 10001:10001
WORKDIR /work

# CMD, not ENTRYPOINT: any arguments a user supplies replace this entirely, so
# both `IMAGE portfolio-backtest` and `IMAGE bash` work with no wrapper.
CMD ["portfolio", "--help"]
