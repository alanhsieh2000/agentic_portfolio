"""Tests for src/flow/cli.py's two interactive loops - the `user_provided`
selection's candidate-confirmation loop and the shared post-run edit loop.

These loops are thin `input()` shells over logic tested elsewhere
(`validate_and_edit_candidates`/`edit_candidates` in
`tests/test_interactive_flow.py`, `save_candidate_pool` in
`tests/test_candidate_memory.py`), so what is tested here is only the
sequencing those shells are responsible for: when a pool is persisted, when
an edit is refused, and which selections touch the candidate memory at all.
Scripted input is fed via `builtins.input`; `validate_and_ingest_tickers`
(the one call that would reach yfinance) and the optimizer step are
monkeypatched, per AGENTS.md.
"""

from datetime import date
from unittest.mock import MagicMock

import pytest

from src.flow.cli import _run_edit_loop, _run_user_provided_confirm_loop

REBALANCE_DATE = date(2026, 9, 5)


def _script(monkeypatch, *responses: str) -> None:
    """Feed `responses` to successive `input()` calls, ignoring the prompt
    each call passes in.
    """
    remaining = iter(responses)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(remaining))


def _fake_ingestion(monkeypatch, invalid: set[str] = frozenset()) -> None:
    """Stand in for the yfinance round trip: every ticker resolves except
    those named in `invalid`.
    """

    def fake(tickers, as_of, db_path):
        cleaned = sorted({t.strip().upper() for t in tickers if t.strip()})
        bad = {t: "no data" for t in cleaned if t in invalid}
        return [t for t in cleaned if t not in bad], bad

    monkeypatch.setattr("src.flow.cli.validate_and_ingest_tickers", fake)
    monkeypatch.setattr("src.flow.interactive.validate_and_ingest_tickers", fake)


# ---------------------------------------------------------------------------
# _run_user_provided_confirm_loop
# ---------------------------------------------------------------------------


def test_confirm_loop_add_then_done_persists_pool_once(monkeypatch):
    _fake_ingestion(monkeypatch)
    save_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", save_spy)
    _script(monkeypatch, "a", "AAPL MSFT", "d")

    pool = _run_user_provided_confirm_loop([], REBALANCE_DATE, "session.duckdb", memory_path="mem.json")

    assert pool == ["AAPL", "MSFT"]
    assert save_spy.call_count == 1
    assert save_spy.call_args.args[0] == ["AAPL", "MSFT"]


def test_confirm_loop_reports_bad_ticker_and_still_adds_the_good_ones(monkeypatch, capsys):
    _fake_ingestion(monkeypatch, invalid={"ZZZZ"})
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", MagicMock())
    _script(monkeypatch, "a", "AAPL ZZZZ", "d")

    pool = _run_user_provided_confirm_loop([], REBALANCE_DATE, "session.duckdb", memory_path="mem.json")

    out = capsys.readouterr().out
    assert pool == ["AAPL"]
    assert "Added: AAPL." in out
    assert "Ignored (not found): ZZZZ." in out


def test_confirm_loop_rejects_finishing_with_an_empty_pool(monkeypatch, capsys):
    _fake_ingestion(monkeypatch)
    save_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", save_spy)
    _script(monkeypatch, "d", "a", "AAPL", "d")

    pool = _run_user_provided_confirm_loop([], REBALANCE_DATE, "session.duckdb", memory_path="mem.json")

    assert "add at least one ticker" in capsys.readouterr().out
    assert pool == ["AAPL"]
    assert save_spy.call_count == 1


def test_confirm_loop_shows_the_persisted_pool_and_can_remove_from_it(monkeypatch, capsys):
    _fake_ingestion(monkeypatch)
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", MagicMock())
    _script(monkeypatch, "r", "MSFT", "d")

    pool = _run_user_provided_confirm_loop(["AAPL", "MSFT"], REBALANCE_DATE, "session.duckdb", memory_path="mem.json")

    out = capsys.readouterr().out
    assert "Current candidate pool (2): AAPL, MSFT" in out
    assert pool == ["AAPL"]


def test_confirm_loop_names_a_removal_that_was_not_in_the_pool(monkeypatch, capsys):
    _fake_ingestion(monkeypatch)
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", MagicMock())
    _script(monkeypatch, "r", "GHOST", "d")

    pool = _run_user_provided_confirm_loop(["AAPL"], REBALANCE_DATE, "session.duckdb", memory_path="mem.json")

    assert "Not in pool (ignored): GHOST." in capsys.readouterr().out
    assert pool == ["AAPL"]


def test_confirm_loop_refuses_an_edit_that_would_empty_the_pool(monkeypatch, capsys):
    _fake_ingestion(monkeypatch)
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", MagicMock())
    _script(monkeypatch, "r", "AAPL", "d")

    pool = _run_user_provided_confirm_loop(["AAPL"], REBALANCE_DATE, "session.duckdb", memory_path="mem.json")

    assert "would be empty" in capsys.readouterr().out
    assert pool == ["AAPL"]


def test_confirm_loop_drops_a_saved_ticker_that_no_longer_resolves(monkeypatch, capsys):
    _fake_ingestion(monkeypatch, invalid={"DEAD"})
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", MagicMock())
    _script(monkeypatch, "d")

    pool = _run_user_provided_confirm_loop(["AAPL", "DEAD"], REBALANCE_DATE, "session.duckdb", memory_path="mem.json")

    out = capsys.readouterr().out
    assert "no longer resolve: DEAD" in out
    assert pool == ["AAPL"]


def test_confirm_loop_reprompts_on_an_unrecognized_choice(monkeypatch, capsys):
    _fake_ingestion(monkeypatch)
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", MagicMock())
    _script(monkeypatch, "x", "d")

    pool = _run_user_provided_confirm_loop(["AAPL"], REBALANCE_DATE, "session.duckdb", memory_path="mem.json")

    assert "Unrecognized choice 'x'." in capsys.readouterr().out
    assert pool == ["AAPL"]


def test_confirm_loop_blank_input_confirms_the_pool(monkeypatch):
    _fake_ingestion(monkeypatch)
    save_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", save_spy)
    _script(monkeypatch, "")

    pool = _run_user_provided_confirm_loop(["AAPL"], REBALANCE_DATE, "session.duckdb", memory_path="mem.json")

    assert pool == ["AAPL"]
    assert save_spy.call_count == 1


# ---------------------------------------------------------------------------
# _run_edit_loop
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_optimizer(monkeypatch):
    """`_run_edit_loop` recomputes weights after every accepted edit; that
    chain is tested for real in tests/test_optimizer.py, so stub it here.
    """
    monkeypatch.setattr(
        "src.flow.cli.compute_weights_and_allocation",
        lambda candidates, objective, value, rebalance_date, db_path: ({}, ({}, 0.0)),
    )
    monkeypatch.setattr("src.flow.cli.print_weights_and_allocation", lambda weights, allocation: None)


def test_run_edit_loop_user_provided_persists_each_accepted_edit(monkeypatch, stub_optimizer):
    _fake_ingestion(monkeypatch)
    save_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", save_spy)
    _script(monkeypatch, "a", "NVDA", "r", "AAPL", "f")

    _run_edit_loop(
        ["AAPL"], "GMV", 1000.0, REBALANCE_DATE, "session.duckdb",
        selection="user_provided", memory_path="mem.json",
    )

    assert save_spy.call_count == 2
    assert save_spy.call_args_list[0].args[0] == ["AAPL", "NVDA"]
    assert save_spy.call_args_list[1].args[0] == ["NVDA"]


def test_run_edit_loop_user_provided_validates_added_tickers(monkeypatch, stub_optimizer, capsys):
    _fake_ingestion(monkeypatch, invalid={"ZZZZ"})
    save_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", save_spy)
    _script(monkeypatch, "a", "NVDA ZZZZ", "f")

    _run_edit_loop(
        ["AAPL"], "GMV", 1000.0, REBALANCE_DATE, "session.duckdb",
        selection="user_provided", memory_path="mem.json",
    )

    assert "Ignored (not found): ZZZZ." in capsys.readouterr().out
    assert save_spy.call_args.args[0] == ["AAPL", "NVDA"]


def test_run_edit_loop_non_user_provided_never_touches_candidate_memory(monkeypatch, stub_optimizer):
    """An LLM scan's candidate list is one run's ephemeral output; only the
    user's own pool is persisted.
    """
    ingest_spy = MagicMock()
    save_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.validate_and_ingest_tickers", ingest_spy)
    monkeypatch.setattr("src.flow.interactive.validate_and_ingest_tickers", ingest_spy)
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", save_spy)
    _script(monkeypatch, "a", "NVDA", "f")

    _run_edit_loop(["AAPL"], "GMV", 1000.0, REBALANCE_DATE, "session.duckdb", selection="llm_s_only")

    assert save_spy.called is False
    assert ingest_spy.called is False


def test_run_edit_loop_keeps_previous_list_when_an_edit_would_empty_it(monkeypatch, stub_optimizer, capsys):
    """The emptied list must be reverted, not merely reported - otherwise a
    later edit (or, for user_provided, a save) would act on it.
    """
    _fake_ingestion(monkeypatch)
    save_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", save_spy)
    _script(monkeypatch, "r", "AAPL", "r", "GHOST", "f")

    _run_edit_loop(
        ["AAPL"], "GMV", 1000.0, REBALANCE_DATE, "session.duckdb",
        selection="user_provided", memory_path="mem.json",
    )

    assert "Candidate list is empty" in capsys.readouterr().out
    # The refused removal is never persisted; the following no-op removal
    # still sees the reverted ["AAPL"].
    assert save_spy.call_count == 1
    assert save_spy.call_args.args[0] == ["AAPL"]
