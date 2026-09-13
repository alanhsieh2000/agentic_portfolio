# Repository Guidelines

## Project Structure & Module Organization
Keep application code under `src/` and organize related Yahoo Finance logic into focused modules or subpackages. The importable package is `src/agentic_portfolio/`, so modules import as `agentic_portfolio.<subpackage>.<module>` — a standard src-layout, which is also the layout `CREWAI.md` documents as canonical for a CrewAI project. Place tests under `tests/`, using one test file per module where practical. Place ExecPlans under `plans/`, using one plan file like `01_*.md` per major step of the project. Root-level project files include `README.md` for project intent, `pyproject.toml` for Python dependencies and package metadata, `Dockerfile` for the **released** container image, and `CREWAI.md` for the agent framework implementation reference. The **development** container is a separate file, `docker/Dockerfile.dev`, because the two optimize for opposite things — the development image installs the dependencies but deliberately not the project itself, so edits take effect without reinstalling; see `docker/README.md`. `.devcontainer/devcontainer.json` is intentionally local-only and gitignored: it hardcodes one machine's absolute bind-mount path, so it is not shareable as written.

## Build, Test, and Development Commands
Set up dependencies are already done and provided as the container. Run the test suite with `uv run pytest tests/test_*.py`. If you use VS Code Dev Containers, open the repository with your own local `.devcontainer` config (gitignored — write one pointing at `docker/Dockerfile.dev`) instead of recreating the environment manually.

Note `pyproject.toml` sets no pytest `pythonpath`: tests import the *installed* package, which `uv run` provides, so that a packaging mistake fails in the suite rather than at a user. Beware that `--help` is not safe on every console script — only `portfolio`, `portfolio-holdings`, `portfolio-summary` and `portfolio-migrate-candidates` parse arguments; the eight `portfolio-build-*`/`portfolio-backfill-snapshot` commands ignore argv and start writing to the market-data database immediately, and `portfolio-backtest` runs a full backtest. To check the entry points are wired up, import them rather than invoking them.

## Coding Style & Naming Conventions
Target Python 3.12 to stay aligned with the `Dockerfile`. Use 4-space indentation, snake_case for modules, functions, and variables, and PascalCase for classes. Keep files and class names descriptive, for example `src/agentic_portfolio/watchlist/downloader.py` or `HistoricalPriceClient`. Isolate Yahoo Finance request handling from data transformation logic so API changes are easier to contain.

## Testing Guidelines
Write tests with the library `pytest` framework. Name test files `test_*.py` and test methods `test_*`. Add coverage for each new parsing, normalization, or fetch workflow, and prefer deterministic fixtures or mocks when network responses are involved. Keep external API calls out of unit tests unless a test is explicitly marked as an integration check.

## Commit & Pull Request Guidelines
Unless asked by the user, you are supposed not to commit and to make changes to git. This repository does not yet have commit history, so use short imperative commit messages such as `Add ETF quote downloader`. Keep commits focused on one change. Pull requests should explain the behavior change, list the commands run for verification, and include sample output or payload notes when market-data handling changes.

## Security & Configuration Tips
Do not hardcode credentials, cookies, or local watchlist data in tracked files. Treat Yahoo Finance responses as unstable input: validate expected fields, handle missing data defensively, and document any required environment-specific configuration in `README.md`.

# ExecPlans
When writing complex features or significant refactors, use an ExecPlan (as described in PLANS.md) from design to implementation.
