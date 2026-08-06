"""Static historical financial-news archive for LLM-F, sourced from the
Hugging Face dataset `KrossKinetic/SP500-Financial-News-Articles-Time-Series`
(https://huggingface.co/datasets/KrossKinetic/SP500-Financial-News-Articles-Time-Series,
MIT-licensed, itself a re-hosting of a Kaggle scrape).

This exists because `yfinance`'s `.news` accessor (see
`plans/03_llm_f_agent.md`) is known to skew toward recent articles and
cannot retroactively return, say, March 2021's headlines when called in
2026. This archive has real historical `Publishdate` values as far back as
2006, verified live: 4,589 rows, 469 distinct tickers, dates spanning
2006-12-04 through 2024-04-20, no null `Publishdate`/`symbol` values. Its
end date lines up closely with `settings.rebalance_end`/`fetch_end`
("2024-04-30"), consistent with this being the same dataset the reference
paper (arXiv:2603.23300) used for its 2020-2024 evaluation window.

Per-ticker density is low and uneven (most tickers cap out at exactly 10
articles across the entire 2006-2024 span) — whether this is enough real
2020-2024 coverage to matter is measured separately, not assumed here; see
`plans/03_llm_f_agent.md`'s Surprises & Discoveries.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb
import requests

from src.config.settings import settings

logger = logging.getLogger(__name__)

PARQUET_URL = (
    "https://huggingface.co/api/datasets/KrossKinetic/SP500-Financial-News-Articles-Time-Series"
    "/parquet/default/train/0.parquet"
)


def download_news_archive(
    dest_path: str = "data/news_archive_source.parquet",
    url: str = PARQUET_URL,
    timeout: float = settings.http_timeout_seconds,
) -> Path:
    """Download the archive's Parquet file to `dest_path`, skipping the
    download if a file already exists there (this is a static, versioned
    third-party dataset, not a live feed — re-downloading on every run
    would be pure waste).
    """
    path = Path(dest_path)
    if path.exists():
        logger.info("news archive already downloaded at %s, skipping fetch", path)
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    path.write_bytes(response.content)
    logger.info("downloaded news archive to %s (%d bytes)", path, len(response.content))
    return path


def build_news_archive(
    parquet_path: str = "data/news_archive_source.parquet", db_path: str = settings.db_path
) -> None:
    """Load the local Parquet file at `parquet_path` into the
    `news_articles_hf` table in the DuckDB file at `db_path`, creating the
    parent directory if needed. Drops any pre-existing table first, so
    re-running this is always safe.

    Logs the real columns/dtypes found in the Parquet file before loading,
    rather than assuming the dataset card's advertised schema — the same
    discipline `plans/03_llm_f_agent.md` applies to `yfinance`'s response
    shape, since either can change out from under this code.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(db_path)
    try:
        described = con.sql(f"DESCRIBE SELECT * FROM read_parquet('{parquet_path}')").df()
        logger.info(
            "news archive parquet columns: %s",
            list(zip(described["column_name"], described["column_type"])),
        )
        con.execute("DROP TABLE IF EXISTS news_articles_hf")
        con.execute(
            "CREATE TABLE news_articles_hf AS "
            "SELECT id_, links, symbol::VARCHAR AS symbol, company, Title AS title, "
            "Text AS body, Publishdate::TIMESTAMP AS publish_date "
            f"FROM read_parquet('{parquet_path}')"
        )
        row_count = con.sql("SELECT count(*) FROM news_articles_hf").fetchone()[0]
        logger.info("wrote %d rows to %s::news_articles_hf", row_count, db_path)
    finally:
        con.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    download_news_archive()
    build_news_archive()
