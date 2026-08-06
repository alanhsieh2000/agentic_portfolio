"""Single source of truth for every environment-configurable runtime value
in this project: the DuckDB path, fetch/rebalance date windows, batch
sizes, rate-limit pause seconds, HTTP timeouts, the book-equity reporting
lag, the LLM model name, and the `SEC_UA`/Anthropic environment variables.

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

    llm_s_model: str = "anthropic/claude-sonnet-4-5"

    sec_ua: str = ""
    anthropic_api_key: str | None = None
    anthropic_base_url: str | None = None


settings = Settings()
