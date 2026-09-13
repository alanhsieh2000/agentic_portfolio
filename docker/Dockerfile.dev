# Use the official Node image to get the binaries
FROM node:24.15.0-bookworm-slim AS node_base

# common base
FROM python:3.12.3-slim-bookworm AS base

LABEL maintainer="Alan Hsieh"

# Copy Node.js and npm from the official image
COPY --from=node_base /usr/local/bin/node /usr/local/bin/
COPY --from=node_base /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# development stage: --target development
FROM base AS development

# use docker build --build-arg USER_NAME=user_name --build-arg USER_UID=uid --target development -t tag .
ARG USER_NAME=root
ARG USER_UID=0

# create the user for the slim(Debian) based OS
RUN groupadd -g ${USER_UID} ${USER_NAME} \
    && useradd -u ${USER_UID} -m -s /bin/bash -g ${USER_NAME} ${USER_NAME}

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    git \
    libatomic1 \
    sqlite3 \
    curl \
    libsqlite3-dev \
    bubblewrap \
    build-essential \ 
    python3-dev && \
    rm -rf /var/lib/apt/lists/*

# install claude code
RUN npm i -g @anthropic-ai/claude-code

WORKDIR /app

# install required python packages
# use uv init --bare, and then uv add -r requirements.txt to generate pyproject.toml and uv.lock
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project
ENV PATH="/app/.venv/bin:$PATH"

# (Optional) Disable the auto-updater to pin the current version
ENV DISABLE_AUTOUPDATER=true

USER ${USER_NAME}
# usage: docker run -it --rm -u $(id -u):$(id -g) -v $HOME/[project root]:/app/[project root] tag /bin/bash
