# Building and publishing the image

Two Dockerfiles live in this project and they are deliberately not shared:

- `Dockerfile` at the repository root is the **release image**. It installs the
  project, runs as a non-root user, bundles the market-data database, and
  carries nothing else.
- `docker/Dockerfile.dev` is the **development container**. It installs the
  project's dependencies but deliberately *not* the project itself, so edits
  take effect without reinstalling, and it carries node plus a coding agent.

Sharing a base between them would drag node into the release image, or the
read-only-dataset and non-root semantics into the development loop. They
optimize for opposite things. The development file is kept here under `docker/`
rather than in `.devcontainer/` for one practical reason: `.devcontainer` is
listed in `.gitignore`, so a Dockerfile placed there would stop being version
controlled.

## Prerequisite: the dataset is not in git

`data/portfolio.duckdb` is gitignored, so **this image cannot be built from a
clean clone.** That is a deliberate limitation, not an oversight. Either:

- build the dataset yourself with the `portfolio-build-*` commands documented in
  `README.md`, or
- obtain the file and verify it against `docker/dataset.sha256`.

The build refuses to proceed unless the file's checksum matches the
`DATASET_SHA256` build argument, so baking the wrong dataset fails the build
rather than shipping quietly.

The same fact is why there is **no CI workflow** for this image: a GitHub Actions
runner has no way to obtain the dataset. Automating the build would require
solving dataset hosting first, which is a separate decision.

## Build locally

Run from the repository root, not from this directory:

    SHA=$(cut -d' ' -f1 docker/dataset.sha256)
    docker build --build-arg DATASET_SHA256="$SHA" -t agentic-portfolio .

## Verify before pushing

The last check is the important one and must run **before** the first push. A
file added in one layer and deleted in a later one still exists in the earlier
layer and is still downloaded by everyone who pulls the image, so listing the
final filesystem is not sufficient.

    IMG=agentic-portfolio

    # the scripts exist and the package imports
    docker run --rm $IMG bash -lc 'ls /opt/agentic-portfolio/venv/bin | grep -c "^portfolio"'   # 13
    docker run --rm $IMG python -c 'import agentic_portfolio; print("ok")'

    # the bundled dataset is found, read-only, and intact
    docker run --rm $IMG bash -lc 'ls -l $DB_PATH'            # mode must be -r--r--r--
    docker run --rm $IMG bash -lc 'sha256sum $DB_PATH'        # matches docker/dataset.sha256
    docker run --rm $IMG cat /opt/agentic-portfolio/data/DATASET.json
    docker run --rm $IMG bash -lc ': > $DB_PATH'; echo "expect non-zero: $?"

    # the paper backtest, with no dataset mounted
    docker run --rm -e ANTHROPIC_API_KEY $IMG portfolio-backtest

    # a missing key refuses in about a second, exit 2, no traceback
    docker run --rm -e ANTHROPIC_API_KEY= $IMG portfolio-backtest; echo "exit=$?"

    # an interactive run writes into the host mount, owned by the host user
    mkdir -p work
    docker run -it --rm --user "$(id -u):$(id -g)" -v "$PWD/work:/work" -w /work \
        $IMG portfolio --date today --value 10000 --selection user_provided
    ls -ln work/output/*/ work/memory/     # uid/gid must be yours, not 0 and not 10001

    # NO secrets and nothing personal in any layer
    docker create --name chk $IMG >/dev/null
    docker export chk | tar -tf - \
      | grep -E '(^|/)(\.env|memory/|output/|holdings\.duckdb|news_archive_source\.parquet|\.git/|\.venv/)' \
      && echo LEAK || echo CLEAN
    docker export chk | grep -aoE 'sk-ant-[A-Za-z0-9_-]{20,}|sk-proj-[A-Za-z0-9_-]{20,}' | head
    docker rm chk >/dev/null

    docker save $IMG -o /tmp/img.tar && mkdir -p /tmp/imgx && tar -xf /tmp/img.tar -C /tmp/imgx
    find /tmp/imgx -name '*.tar*' -exec sh -c 'tar -tf "$1" 2>/dev/null' _ {} \; \
      | grep -E '\.env$|memory/|output/|holdings\.duckdb' && echo LEAK || echo CLEAN

The content scan is the gate. Filenames can be renamed; key material cannot be
disguised. Once a layer carrying a key reaches a registry, deleting the tag does
not reliably delete the blob, and the key must be rotated.

## Publish, multi-arch, to GHCR

    docker buildx create --name apx --driver docker-container --use --bootstrap
    echo "$GHCR_TOKEN" | docker login ghcr.io -u <owner> --password-stdin

    SHA=$(cut -d' ' -f1 docker/dataset.sha256)
    VERSION=$(grep -m1 '^version' pyproject.toml | cut -d'"' -f2)

    docker buildx build --platform linux/amd64,linux/arm64 \
      --build-arg DATASET_SHA256="$SHA" \
      --label org.opencontainers.image.source=https://github.com/<owner>/agentic_portfolio \
      --label org.opencontainers.image.version="$VERSION" \
      --label org.opencontainers.image.revision="$(git rev-parse HEAD)" \
      --annotation "index:org.opencontainers.image.description=Agentic AI screening for portfolio investment, with bundled market data" \
      -t "ghcr.io/<owner>/agentic-portfolio:$VERSION" \
      -t "ghcr.io/<owner>/agentic-portfolio:${VERSION%.*}" \
      -t "ghcr.io/<owner>/agentic-portfolio:latest" \
      -t "ghcr.io/<owner>/agentic-portfolio:sha-$(git rev-parse --short HEAD)" \
      --provenance=true --sbom=true --push .

Three things that are easy to get wrong here:

- **`--annotation index:` is not the same as `--label`.** GHCR's package page
  reads the image *index* annotation; labels alone leave the listing
  description blank.
- **The package is private by default.** After the first push, make it public
  once in the GHCR web interface, or every `docker pull` fails with a 401 and
  the whole distribution story breaks silently.
- **Bump `version` in `pyproject.toml` for anything that changes image content,
  including a dataset refresh.** Re-pushing different data under an existing tag
  is the one thing that makes `:0.1.0` a lie.

If the emulated arm64 build stalls compiling a source distribution somewhere in
the crewai dependency tail, build each architecture natively and join them:

    docker buildx imagetools create -t ghcr.io/<owner>/agentic-portfolio:$VERSION \
        ghcr.io/<owner>/agentic-portfolio:$VERSION-amd64 \
        ghcr.io/<owner>/agentic-portfolio:$VERSION-arm64

## Refreshing the bundled dataset

The backtest window is fixed history, so the dataset does not go stale and needs
no routine refresh. If you rebuild it anyway:

    portfolio-build-membership && portfolio-build-prices && \
      portfolio-build-fundamentals && portfolio-build-momentum && \
      portfolio-build-returns && portfolio-build-dividends && \
      portfolio-build-news-archive
    sha256sum data/portfolio.duckdb | awk '{print $1"  portfolio.duckdb"}' > docker/dataset.sha256

Then bump the version and rebuild. `DATASET.json` is regenerated from the file
during the build, so it cannot drift.
