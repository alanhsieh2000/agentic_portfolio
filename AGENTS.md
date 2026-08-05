# Repository Guidelines

## Project Structure & Module Organization
Keep application code under `src/` and organize related Yahoo Finance logic into focused modules or subpackages. Place tests under `tests/`, using one test file per module where practical. Place ExecPlans under `plans/`, using one plan file like `01_*.md` per major step of the project. Root-level project files include `README.md` for project intent, `pyproject.toml` for Python dependencies, `Dockerfile` for the container image, `.devcontainer/devcontainer.json` for the preferred development environment, and `CREWAI.md` for the agent framework implementation reference.

## Build, Test, and Development Commands
Set up dependencies are already done and provided as the container. Run the test suite with `uv run pytest tests/test_*.py`. If you use VS Code Dev Containers, open the repository with the existing `.devcontainer` config instead of recreating the environment manually.

## Coding Style & Naming Conventions
Target Python 3.12 to stay aligned with the `Dockerfile`. Use 4-space indentation, snake_case for modules, functions, and variables, and PascalCase for classes. Keep files and class names descriptive, for example `src/watchlist/downloader.py` or `HistoricalPriceClient`. Isolate Yahoo Finance request handling from data transformation logic so API changes are easier to contain.

## Testing Guidelines
Write tests with the library `pytest` framework. Name test files `test_*.py` and test methods `test_*`. Add coverage for each new parsing, normalization, or fetch workflow, and prefer deterministic fixtures or mocks when network responses are involved. Keep external API calls out of unit tests unless a test is explicitly marked as an integration check.

## Commit & Pull Request Guidelines
Unless asked by the user, you are supposed not to commit and to make changes to git. This repository does not yet have commit history, so use short imperative commit messages such as `Add ETF quote downloader`. Keep commits focused on one change. Pull requests should explain the behavior change, list the commands run for verification, and include sample output or payload notes when market-data handling changes.

## Security & Configuration Tips
Do not hardcode credentials, cookies, or local watchlist data in tracked files. Treat Yahoo Finance responses as unstable input: validate expected fields, handle missing data defensively, and document any required environment-specific configuration in `README.md`.

# ExecPlans
When writing complex features or significant refactors, use an ExecPlan (as described in PLANS.md) from design to implementation.
