"""Tests for src/dataset/news_archive.py.

Per AGENTS.md, no test here downloads the real Hugging Face parquet file;
`build_news_archive` is exercised against a small hand-written Parquet
fixture written with DuckDB's own COPY TO (no pyarrow dependency needed),
and `download_news_archive`'s skip-if-exists path is checked without ever
letting `requests.get` be called.
"""

import duckdb
import pytest

from src.dataset.news_archive import build_news_archive, download_news_archive


def _write_fixture_parquet(path) -> None:
    """Write a tiny Parquet file matching the real archive's schema
    (id_, links, symbol, company, Title, Text, Publishdate) using DuckDB's
    own Parquet writer, so this test needs no pyarrow dependency.
    """
    con = duckdb.connect()
    con.execute(
        """
        CREATE TABLE fixture AS SELECT * FROM (VALUES
            (0, 'https://example.com/a', 'AAPL', 'Apple Inc.', 'Apple headline one',
             'body text one', TIMESTAMP '2020-03-15 00:00:00'),
            (1, 'https://example.com/b', 'MSFT', 'Microsoft Corp.', 'Microsoft headline',
             'body text two', TIMESTAMP '2022-11-02 00:00:00')
        ) AS t(id_, links, symbol, company, Title, Text, Publishdate)
        """
    )
    con.execute(f"COPY fixture TO '{path}' (FORMAT PARQUET)")
    con.close()


def test_build_news_archive_renames_and_types_columns(tmp_path):
    parquet_path = tmp_path / "fixture.parquet"
    db_path = tmp_path / "test.duckdb"
    _write_fixture_parquet(parquet_path)

    build_news_archive(parquet_path=str(parquet_path), db_path=str(db_path))

    con = duckdb.connect(str(db_path), read_only=True)
    rows = con.sql(
        "SELECT id_, symbol, title, body, publish_date FROM news_articles_hf ORDER BY id_"
    ).fetchall()
    con.close()

    assert rows == [
        (0, "AAPL", "Apple headline one", "body text one", __import__("datetime").datetime(2020, 3, 15)),
        (1, "MSFT", "Microsoft headline", "body text two", __import__("datetime").datetime(2022, 11, 2)),
    ]


def test_build_news_archive_is_idempotent(tmp_path):
    parquet_path = tmp_path / "fixture.parquet"
    db_path = tmp_path / "test.duckdb"
    _write_fixture_parquet(parquet_path)

    build_news_archive(parquet_path=str(parquet_path), db_path=str(db_path))
    build_news_archive(parquet_path=str(parquet_path), db_path=str(db_path))

    con = duckdb.connect(str(db_path), read_only=True)
    count = con.sql("SELECT count(*) FROM news_articles_hf").fetchone()[0]
    con.close()
    assert count == 2


def test_download_news_archive_skips_fetch_when_file_exists(tmp_path, monkeypatch):
    dest = tmp_path / "already_here.parquet"
    dest.write_bytes(b"not a real parquet file, just needs to exist")

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("requests.get must not be called when the destination already exists")

    monkeypatch.setattr("src.dataset.news_archive.requests.get", _fail_if_called)

    result = download_news_archive(dest_path=str(dest))
    assert result == dest
