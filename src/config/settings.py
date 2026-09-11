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
`src/dataset/sec_edgar.py`.

Deliberately NOT centralized here: URLs and schema-alias lists tied to an
external data source's actual shape (`src/dataset/membership.py`'s
`WIKIPEDIA_URL`, `src/dataset/sec_edgar.py`'s SEC URLs and
`BOOK_EQUITY_XBRL_TAGS`, `src/dataset/fundamentals.py`'s
`BOOK_EQUITY_ALIASES`) — these aren't values an operator tunes per
environment; changing them requires code-level awareness of the external
source's schema regardless of where the literal lives.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Every tunable runtime knob and environment variable this project
    depends on. `anthropic_api_key`/`anthropic_base_url` are documented
    here for completeness even though no code in `src/` reads them
    directly — they're consumed by the `anthropic` SDK underneath `crewai`
    straight from the process environment, not by this project's own code.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    db_path: str = "data/portfolio.duckdb"

    output_dir: str = "output"
    """Directory the printed portfolio reports are archived under, one
    subdirectory per month of the run's as-of date (see
    `src/flow/report_archive.py`). Override with OUTPUT_DIR, or per run with
    `--output-dir` on either entry point."""

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

    llm_quick: str = "openai/gpt-5-nano"
    """Small, cheap model for the high-volume prose passes - currently only
    the monthly report summarizer in `src/agents/report_summary.py`. It is a
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
    alone. `src/agents/report_summary.py` therefore checks this field first
    and the environment second."""


settings = Settings()
