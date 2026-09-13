"""Single source of truth for every environment-configurable runtime value
in this project: the DuckDB path, the report archive directory,
fetch/rebalance date windows, batch sizes, rate-limit pause seconds, HTTP
timeouts, the book-equity reporting lag, the trailing dividend-yield
window, the transaction cost and risk-free rate used by Backtest Mode
Stage 2, the two LLM model names, and the `SEC_UA`/Anthropic/OpenAI
environment variables.

`transaction_cost_bps` and `risk_free_rate` are consumed by
`plans/06_interactive_flow.md`'s backtest runner (not yet implemented as
of this field's addition - see that plan's Progress section), per
`README.md`'s Backtest Mode Stage 2 defaults of 10 basis points and 2%.

`Settings` is a `pydantic_settings.BaseSettings` subclass, loaded once as
the module-level singleton `settings` below. Field names match env var
names case-insensitively (pydantic-settings' default), so `sec_ua` reads
`SEC_UA` and `llm_s_model` reads `LLM_S_MODEL` with no explicit alias
needed. `.env` (already gitignored — confirmed via `git check-ignore`) is
loaded automatically at import time via `env_file=".env"`, replacing the
one `load_dotenv()` call this project previously made lazily inside
`src/agentic_portfolio/dataset/sec_edgar.py`.

Deliberately NOT centralized here: URLs and schema-alias lists tied to an
external data source's actual shape (`src/agentic_portfolio/dataset/membership.py`'s
`WIKIPEDIA_URL`, `src/agentic_portfolio/dataset/sec_edgar.py`'s SEC URLs and
`BOOK_EQUITY_XBRL_TAGS`, `src/agentic_portfolio/dataset/fundamentals.py`'s
`BOOK_EQUITY_ALIASES`) — these aren't values an operator tunes per
environment; changing them requires code-level awareness of the external
source's schema regardless of where the literal lives.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_HOME = os.environ.get("AGENTIC_PORTFOLIO_HOME", ".")
"""Directory every relative runtime path below is resolved against. Defaults
to the current working directory, which is the historical behavior and the
right one for a container that mounts a workspace and sets its own `-w`.

Read from `os.environ` rather than declared as a field because `model_config`
below needs it, and a class body cannot see its own field values."""


def _under_home(relative: str) -> str:
    """`relative` resolved against `_HOME`, unchanged when `_HOME` is unset.

    The identity branch is load-bearing rather than cosmetic: returning
    `./data/portfolio.duckdb` instead of `data/portfolio.duckdb` would change
    every path this project prints to the user and break the tests that assert
    the exact default string. So the no-override case stays byte-identical.
    """
    return relative if _HOME in ("", ".") else str(Path(_HOME) / relative)


class Settings(BaseSettings):
    """Every tunable runtime knob and environment variable this project
    depends on. `anthropic_api_key`/`anthropic_base_url` are documented
    here for completeness even though no code in `src/agentic_portfolio/` reads them
    directly — they're consumed by the `anthropic` SDK underneath `crewai`
    straight from the process environment, not by this project's own code.
    """

    model_config = SettingsConfigDict(
        env_file=_under_home(".env"), env_file_encoding="utf-8", extra="ignore"
    )

    agentic_portfolio_home: str = _HOME
    """Where the relative paths below are rooted, as an introspectable field so
    a report can say which workspace it read. Deliberately NOT named `home`:
    `case_sensitive` is unset, so pydantic-settings matches field names to
    environment variables case-insensitively and a field called `home` would
    silently absorb `$HOME`, moving every path into the user's home directory.

    Note one limit. CrewAI calls `load_dotenv()` itself, resolving `.env` from
    the CURRENT WORKING DIRECTORY, which is how an LLM API key in `.env`
    reaches litellm at all. Point this setting away from the working directory
    and the two readers look in different places, so keep the LLM keys in the
    real environment when doing that."""

    db_path: str = _under_home("data/portfolio.duckdb")
    """The shared market-data cache: prices, returns, factors, membership,
    dividends and news. Override with DB_PATH - which the Docker image does, to
    reach a read-only dataset baked into an image layer while everything
    writable still follows the working directory."""

    output_dir: str = _under_home("output")
    """Directory the printed portfolio reports are archived under, one
    subdirectory per month of the run's as-of date (see
    `src/agentic_portfolio/flow/report_archive.py`). Override with OUTPUT_DIR, or per run with
    `--output-dir` on either entry point."""

    holdings_db_path: str = _under_home("data/holdings.duckdb")
    """Price and monthly-return cache for the tickers the user actually holds.
    Deliberately a different file from `db_path`: the window a candidate pool is
    measured over is derived from every row in that shared table, so reporting
    on your own holdings must never add rows to it."""

    news_archive_path: str = _under_home("data/news_archive_source.parquet")
    """Downloaded source the news-archive builder reads to populate the
    `news_articles_hf` table inside `db_path`."""

    candidates_path: str = _under_home("memory/candidates.json")
    rates_path: str = _under_home("memory/rates.json")
    portfolio_path: str = _under_home("memory/portfolio.json")

    fetch_start: str = "2015-01-01"
    fetch_end: str = "2024-04-30"
    price_batch_size: int = 100

    rebalance_start: str = "2020-01-01"
    rebalance_end: str = "2024-04-30"

    http_timeout_seconds: float = 30.0
    sec_pause_seconds: float = 0.15
    yfinance_price_pause_seconds: float = 1.0
    yfinance_fundamentals_pause_seconds: float = 0.25

    book_equity_lag_months: int = 3

    dividend_lookback_months: int = 12
    """Trailing window for a per-ticker dividend yield: the per-share
    dividends with an ex-date in this many months before the as-of date,
    divided by the latest price. 12 is the trailing-twelve-month
    convention; override with DIVIDEND_LOOKBACK_MONTHS."""

    transaction_cost_bps: float = 10.0
    risk_free_rate: float = 0.02

    llm_s_model: str = "anthropic/claude-sonnet-4-5"

    llm_f_model: str = "anthropic/claude-sonnet-4-5"
    """Model LLM-F scores news sentiment with. Overridden by LLM_F_MODEL, or per
    call by `screen_month`'s `model` argument.

    Declared here, rather than left as the `os.environ.get("LLM_F_MODEL", ...)`
    read it used to be in `src/agentic_portfolio/agents/llm_f.py`, so that the
    preflight check in `src/agentic_portfolio/config/preflight.py` can ask which
    provider LLM-F needs without duplicating the resolution - two copies of that
    logic would eventually disagree, and the preflight would then vouch for a
    key the run does not use."""

    llm_quick: str = "openai/gpt-5-nano"
    """Small, cheap model for the high-volume prose passes - currently only
    the monthly report summarizer in `src/agentic_portfolio/agents/report_summary.py`. It is a
    separate knob from `llm_s_model` because the two calls do different kinds
    of work: LLM-S reasons about data and needs a capable model, while the
    summarizer only writes sentences around figures this project has already
    computed itself. Override with LLM_QUICK, or per run with `--model` on
    `uv run portfolio-summary`."""

    sec_ua: str = ""
    anthropic_api_key: str | None = None
    anthropic_base_url: str | None = None
    openai_api_key: str | None = None
    """Consumed by the OpenAI SDK underneath `crewai` straight from the
    process environment, like the Anthropic pair above - but declared here for
    a second reason that is not decorative. `pydantic_settings` loads `.env`
    WITHOUT exporting it to `os.environ`, so code that checks only
    `os.environ` would refuse to run on a machine whose key lives in `.env`
    alone. `src/agentic_portfolio/agents/report_summary.py` therefore checks this field first
    and the environment second."""


settings = Settings()
