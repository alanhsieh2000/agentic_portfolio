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
monkeypatched, per AGENTS.md. The benchmark is monkeypatched at whichever
seam a test is not about - `prepare_benchmark` when the sequencing around it
is under test, `_settle_benchmark` when only `main`'s wiring is - since
resolving one for real reaches yfinance too.
"""

from contextlib import contextmanager
from datetime import date
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.flow.cli import (
    _choose_pool_to_resume,
    _run_edit_loop,
    _run_user_provided_confirm_loop,
    _settle_benchmark,
    format_benchmark,
    main,
    print_weights_and_allocation,
)
from src.config.settings import settings
from src.optimizer.benchmark import BenchmarkSource, BenchmarkStats
from src.optimizer.holdings import unavailable_holdings
from src.optimizer.portfolio import DEFAULT_TARGET_ANNUAL_RETURN, PortfolioStats

REBALANCE_DATE = date(2026, 9, 5)


def _script(monkeypatch, *responses: str) -> None:
    """Feed `responses` to successive `input()` calls, ignoring the prompt
    each call passes in.
    """
    remaining = iter(responses)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(remaining))


def _fake_ingestion(
    monkeypatch,
    invalid: set[str] = frozenset(),
    currencies: dict[str, str] | None = None,
) -> None:
    """Stand in for the yfinance round trip: every ticker resolves except
    those named in `invalid`, and trades in `currencies[ticker]` or USD.
    """

    def fake(tickers, as_of, db_path):
        cleaned = sorted({t.strip().upper() for t in tickers if t.strip()})
        bad = {t: "no data" for t in cleaned if t in invalid}
        good = [t for t in cleaned if t not in bad]
        return good, bad, {t: (currencies or {}).get(t, "USD") for t in good}

    monkeypatch.setattr("src.flow.cli.validate_and_ingest_tickers", fake)
    monkeypatch.setattr("src.flow.interactive.validate_and_ingest_tickers", fake)
    # `validate_and_edit_candidates` re-derives the pool currency from the
    # database after a removal; there is none in these tests.
    monkeypatch.setattr(
        "src.flow.interactive.load_ticker_currencies",
        lambda tickers, db_path: {t: (currencies or {}).get(t, "USD") for t in tickers},
    )


# ---------------------------------------------------------------------------
# _run_user_provided_confirm_loop
# ---------------------------------------------------------------------------


def test_confirm_loop_add_then_done_persists_pool_once(monkeypatch):
    _fake_ingestion(monkeypatch)
    save_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", save_spy)
    _script(monkeypatch, "a", "AAPL MSFT", "d")

    pool, _currency = _run_user_provided_confirm_loop({}, REBALANCE_DATE, "session.duckdb", memory_path="mem.json")

    assert pool == ["AAPL", "MSFT"]
    assert save_spy.call_count == 1
    assert save_spy.call_args.args[0] == ["AAPL", "MSFT"]


def test_confirm_loop_reports_bad_ticker_and_still_adds_the_good_ones(monkeypatch, capsys):
    _fake_ingestion(monkeypatch, invalid={"ZZZZ"})
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", MagicMock())
    _script(monkeypatch, "a", "AAPL ZZZZ", "d")

    pool, _currency = _run_user_provided_confirm_loop({}, REBALANCE_DATE, "session.duckdb", memory_path="mem.json")

    out = capsys.readouterr().out
    assert pool == ["AAPL"]
    assert "Added: AAPL." in out
    assert "Ignored (not found): ZZZZ." in out


def test_confirm_loop_rejects_finishing_with_an_empty_pool(monkeypatch, capsys):
    _fake_ingestion(monkeypatch)
    save_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", save_spy)
    _script(monkeypatch, "d", "a", "AAPL", "d")

    pool, _currency = _run_user_provided_confirm_loop({}, REBALANCE_DATE, "session.duckdb", memory_path="mem.json")

    assert "add at least one ticker" in capsys.readouterr().out
    assert pool == ["AAPL"]
    assert save_spy.call_count == 1


def test_confirm_loop_shows_the_persisted_pool_and_can_remove_from_it(monkeypatch, capsys):
    _fake_ingestion(monkeypatch)
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", MagicMock())
    _script(monkeypatch, "", "r", "MSFT", "d")

    pool, _currency = _run_user_provided_confirm_loop(
        {"USD": ["AAPL", "MSFT"]}, REBALANCE_DATE, "session.duckdb", memory_path="mem.json")

    out = capsys.readouterr().out
    assert "Current candidate pool (2): AAPL, MSFT" in out
    assert pool == ["AAPL"]


def test_confirm_loop_names_a_removal_that_was_not_in_the_pool(monkeypatch, capsys):
    _fake_ingestion(monkeypatch)
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", MagicMock())
    _script(monkeypatch, "", "r", "GHOST", "d")

    pool, _currency = _run_user_provided_confirm_loop(
        {"USD": ["AAPL"]}, REBALANCE_DATE, "session.duckdb", memory_path="mem.json")

    assert "Not in pool (ignored): GHOST." in capsys.readouterr().out
    assert pool == ["AAPL"]


def test_confirm_loop_refuses_an_edit_that_would_empty_the_pool(monkeypatch, capsys):
    _fake_ingestion(monkeypatch)
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", MagicMock())
    _script(monkeypatch, "", "r", "AAPL", "d")

    pool, _currency = _run_user_provided_confirm_loop(
        {"USD": ["AAPL"]}, REBALANCE_DATE, "session.duckdb", memory_path="mem.json")

    assert "would be empty" in capsys.readouterr().out
    assert pool == ["AAPL"]


def test_confirm_loop_drops_a_saved_ticker_that_no_longer_resolves(monkeypatch, capsys):
    _fake_ingestion(monkeypatch, invalid={"DEAD"})
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", MagicMock())
    _script(monkeypatch, "", "d")

    pool, _currency = _run_user_provided_confirm_loop(
        {"USD": ["AAPL", "DEAD"]}, REBALANCE_DATE, "session.duckdb", memory_path="mem.json")

    out = capsys.readouterr().out
    assert "no longer resolve: DEAD" in out
    assert pool == ["AAPL"]


def test_confirm_loop_reprompts_on_an_unrecognized_choice(monkeypatch, capsys):
    _fake_ingestion(monkeypatch)
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", MagicMock())
    _script(monkeypatch, "", "x", "d")

    pool, _currency = _run_user_provided_confirm_loop(
        {"USD": ["AAPL"]}, REBALANCE_DATE, "session.duckdb", memory_path="mem.json")

    assert "Unrecognized choice 'x'." in capsys.readouterr().out
    assert pool == ["AAPL"]


def test_confirm_loop_blank_input_confirms_the_pool(monkeypatch):
    _fake_ingestion(monkeypatch)
    save_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", save_spy)
    _script(monkeypatch, "", "")

    pool, _currency = _run_user_provided_confirm_loop(
        {"USD": ["AAPL"]}, REBALANCE_DATE, "session.duckdb", memory_path="mem.json")

    assert pool == ["AAPL"]
    assert save_spy.call_count == 1


def test_confirm_loop_first_ticker_establishes_the_pool_currency_and_refuses_others(
    monkeypatch, capsys
):
    """The headline behavior of the currency work: a dollar ticker is refused
    from a yen pool, by name, with the reason, and the pool is untouched.
    """
    _fake_ingestion(monkeypatch, currencies={"7203.T": "JPY"})
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", MagicMock())
    _script(monkeypatch, "a", "7203.T", "a", "AAPL", "d")

    pool, currency = _run_user_provided_confirm_loop({}, REBALANCE_DATE, "session.duckdb", memory_path="mem.json")

    out = capsys.readouterr().out
    assert pool == ["7203.T"]
    assert currency == "JPY"
    assert (
        "Refused: AAPL is priced in USD but this pool is JPY. "
        "A portfolio cannot mix currencies; run them separately." in out
    )


def test_confirm_loop_currency_is_established_by_what_was_typed_first(monkeypatch, capsys):
    """The mirror image: typing the dollar ticker first makes a dollar pool,
    even though '7203.T' sorts ahead of 'AAPL'.
    """
    _fake_ingestion(monkeypatch, currencies={"7203.T": "JPY"})
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", MagicMock())
    _script(monkeypatch, "a", "AAPL 7203.T", "d")

    pool, currency = _run_user_provided_confirm_loop({}, REBALANCE_DATE, "session.duckdb", memory_path="mem.json")

    assert pool == ["AAPL"]
    assert currency == "USD"
    assert "Refused: 7203.T is priced in JPY but this pool is USD." in capsys.readouterr().out


def test_confirm_loop_same_line_partial_accept_across_currencies(monkeypatch, capsys):
    """A refused ticker never blocks a good one typed beside it."""
    _fake_ingestion(monkeypatch, currencies={"7203.T": "JPY", "6758.T": "JPY"})
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", MagicMock())
    _script(monkeypatch, "a", "7203.T", "a", "AAPL 6758.T", "d")

    pool, currency = _run_user_provided_confirm_loop({}, REBALANCE_DATE, "session.duckdb", memory_path="mem.json")

    out = capsys.readouterr().out
    assert pool == ["6758.T", "7203.T"]
    assert currency == "JPY"
    assert "Added: 6758.T." in out
    assert "Refused: AAPL is priced in USD but this pool is JPY." in out


def test_confirm_loop_reports_a_mixed_persisted_pool_and_asks_which_to_keep(monkeypatch, capsys):
    """A pool saved before currencies were recorded can legitimately be
    mixed; the machine cannot know which was intended, so it asks.
    """
    _fake_ingestion(monkeypatch, currencies={"7203.T": "JPY"})
    save_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", save_spy)
    _script(monkeypatch, "", "JPY", "d")

    pool, currency = _run_user_provided_confirm_loop(
        {"USD": ["AAPL", "7203.T"]}, REBALANCE_DATE, "session.duckdb", memory_path="mem.json"
    )

    out = capsys.readouterr().out
    assert "mixes currencies" in out
    assert "  JPY: 7203.T" in out
    assert "  USD: AAPL" in out
    assert "Keeping JPY; dropping AAPL." in out
    assert pool == ["7203.T"]
    assert currency == "JPY"
    # Nothing reaches disk until the pool is confirmed at the [d]one prompt.
    assert save_spy.call_count == 1
    assert save_spy.call_args.args[0] == ["7203.T"]


def test_confirm_loop_reprompts_on_an_unrecognized_currency_choice(monkeypatch, capsys):
    _fake_ingestion(monkeypatch, currencies={"7203.T": "JPY"})
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", MagicMock())
    _script(monkeypatch, "", "EUR", "USD", "d")

    pool, currency = _run_user_provided_confirm_loop(
        {"USD": ["AAPL", "7203.T"]}, REBALANCE_DATE, "session.duckdb", memory_path="mem.json"
    )

    assert "Unrecognized currency 'EUR'." in capsys.readouterr().out
    assert pool == ["AAPL"]
    assert currency == "USD"


# ---------------------------------------------------------------------------
# _choose_pool_to_resume
# ---------------------------------------------------------------------------


def test_choose_pool_to_resume_with_nothing_saved_starts_empty(monkeypatch):
    """No prompt at all on a first-ever run, and no currency yet - the first
    ticker typed will establish it.
    """
    _script(monkeypatch)  # any input() call would raise StopIteration

    assert _choose_pool_to_resume({}) == ([], None)


def test_choose_pool_to_resume_with_one_saved_pool_resumes_on_a_bare_enter(monkeypatch, capsys):
    _script(monkeypatch, "")

    tickers, currency = _choose_pool_to_resume({"JPY": ["6758.T", "7203.T"]})

    assert "  JPY (2): 6758.T, 7203.T" in capsys.readouterr().out
    assert (tickers, currency) == (["6758.T", "7203.T"], "JPY")


def test_choose_pool_to_resume_offers_a_new_pool_even_when_only_one_is_saved(monkeypatch):
    """Without this, someone whose only saved pool is USD could never start a
    JPY one: every Tokyo ticker would be refused against the pool they had
    been placed in with no way out.
    """
    _script(monkeypatch, "n")

    assert _choose_pool_to_resume({"USD": ["AAPL"]}) == ([], None)


def test_choose_pool_to_resume_lists_several_pools_and_resumes_the_chosen_one(monkeypatch, capsys):
    _script(monkeypatch, "JPY")

    tickers, currency = _choose_pool_to_resume({"USD": ["AAPL", "SPY"], "JPY": ["7203.T"]})

    out = capsys.readouterr().out
    assert "Saved candidate pools:" in out
    assert "  USD (2): AAPL, SPY" in out
    assert "  JPY (1): 7203.T" in out
    assert (tickers, currency) == (["7203.T"], "JPY")


def test_choose_pool_to_resume_can_start_a_new_pool_instead(monkeypatch):
    _script(monkeypatch, "n")

    assert _choose_pool_to_resume({"USD": ["AAPL"], "JPY": ["7203.T"]}) == ([], None)


def test_choose_pool_to_resume_reprompts_on_an_unrecognized_currency(monkeypatch, capsys):
    _script(monkeypatch, "EUR", "USD")

    tickers, currency = _choose_pool_to_resume({"USD": ["AAPL"], "JPY": ["7203.T"]})

    assert "Unrecognized choice 'EUR'." in capsys.readouterr().out
    assert (tickers, currency) == (["AAPL"], "USD")


def test_confirm_loop_resumes_a_chosen_pool_and_saves_it_under_its_own_currency(
    monkeypatch, capsys
):
    """End to end for the shared file: two pools saved, the JPY one resumed,
    and the save lands in the JPY slot rather than clobbering USD.
    """
    _fake_ingestion(monkeypatch, currencies={"7203.T": "JPY", "6758.T": "JPY"})
    save_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", save_spy)
    _script(monkeypatch, "JPY", "a", "6758.T", "d")

    pool, currency = _run_user_provided_confirm_loop(
        {"USD": ["AAPL", "SPY"], "JPY": ["7203.T"]},
        REBALANCE_DATE,
        "session.duckdb",
        memory_path="mem.json",
    )

    assert pool == ["6758.T", "7203.T"]
    assert currency == "JPY"
    assert save_spy.call_args.kwargs["currency"] == "JPY"
    assert save_spy.call_args.args[0] == ["6758.T", "7203.T"]
    assert "Saved 2 JPY ticker(s) to mem.json." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _run_edit_loop
# ---------------------------------------------------------------------------


def _stats(**overrides) -> PortfolioStats:
    """A `PortfolioStats` with everything zeroed unless named - enough to
    stand in wherever the value is passed through rather than inspected.
    """
    fields = {
        "weights": {},
        "expected_returns": {},
        "volatility": {},
        "portfolio_expected_return": 0.0,
        "portfolio_volatility": 0.0,
        "portfolio_sharpe": 0.0,
        "risk_free_rate": 0.02,
        "target_annual_return": DEFAULT_TARGET_ANNUAL_RETURN,
        "returns_window_start": date(2020, 5, 1),
        "returns_window_end": date(2025, 4, 1),
        "returns_window_months": 60,
    }
    return PortfolioStats(**{**fields, **overrides})


@pytest.fixture
def stub_optimizer(monkeypatch):
    """`_run_edit_loop` recomputes weights after every accepted edit; that
    chain is tested for real in tests/test_optimizer.py, so stub it here.
    Returns the spy so a test can assert what the loop asked to recompute.
    """
    spy = MagicMock(return_value=(_stats(), ({}, 0.0)))
    monkeypatch.setattr("src.flow.cli.compute_weights_and_allocation", spy)
    monkeypatch.setattr("src.flow.cli.print_weights_and_allocation", lambda *args, **kwargs: None)
    return spy


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


# ---------------------------------------------------------------------------
# _run_edit_loop's target-return command
# ---------------------------------------------------------------------------


def test_run_edit_loop_target_return_explains_itself_when_objective_is_not_mv(
    monkeypatch, stub_optimizer, capsys
):
    """Only MV is defined by a target return, so under the other objectives
    the choice must say so rather than silently accepting a value that
    would not be used.
    """
    _script(monkeypatch, "t", "f")

    _run_edit_loop(["AAPL"], "GMV", 1000.0, REBALANCE_DATE, "session.duckdb")

    assert "applies only to objective MV" in capsys.readouterr().out
    assert stub_optimizer.called is False


def test_run_edit_loop_target_return_updates_the_value_when_objective_is_mv(monkeypatch, stub_optimizer):
    _script(monkeypatch, "t", "0.08", "f")

    _run_edit_loop(["AAPL"], "MV", 1000.0, REBALANCE_DATE, "session.duckdb")

    assert stub_optimizer.call_count == 1
    assert stub_optimizer.call_args.kwargs["target_annual_return"] == 0.08


def test_run_edit_loop_target_return_blank_input_keeps_the_value_and_skips_recompute(
    monkeypatch, stub_optimizer
):
    _script(monkeypatch, "t", "", "f")

    _run_edit_loop(["AAPL"], "MV", 1000.0, REBALANCE_DATE, "session.duckdb", target_annual_return=0.09)

    assert stub_optimizer.called is False


def test_run_edit_loop_target_return_unparseable_input_keeps_the_value(monkeypatch, stub_optimizer, capsys):
    _script(monkeypatch, "t", "not-a-number", "f")

    _run_edit_loop(["AAPL"], "MV", 1000.0, REBALANCE_DATE, "session.duckdb", target_annual_return=0.09)

    assert "Unrecognized target return 'not-a-number'" in capsys.readouterr().out
    assert stub_optimizer.called is False


def test_run_edit_loop_switching_objective_to_mv_prompts_for_a_target_return(monkeypatch, stub_optimizer):
    """Switching to MV is the moment the target starts to matter, so it is
    asked for in the same step rather than silently defaulted.
    """
    _script(monkeypatch, "o", "MV", "0.07", "f")

    _run_edit_loop(["AAPL"], "GMV", 1000.0, REBALANCE_DATE, "session.duckdb")

    assert stub_optimizer.call_count == 1
    assert stub_optimizer.call_args.args[1] == "MV"
    assert stub_optimizer.call_args.kwargs["target_annual_return"] == 0.07


def test_run_edit_loop_switching_objective_to_mv_with_blank_target_keeps_the_default(
    monkeypatch, stub_optimizer
):
    _script(monkeypatch, "o", "MV", "", "f")

    _run_edit_loop(["AAPL"], "GMV", 1000.0, REBALANCE_DATE, "session.duckdb")

    assert stub_optimizer.call_args.kwargs["target_annual_return"] == DEFAULT_TARGET_ANNUAL_RETURN


def test_run_edit_loop_switching_objective_to_gmv_does_not_prompt_for_a_target_return(
    monkeypatch, stub_optimizer
):
    """The scripted input ends right after "GMV"; an unwanted target prompt
    would consume the "f" and leave the loop running.
    """
    _script(monkeypatch, "o", "GMV", "f")

    _run_edit_loop(["AAPL"], "MV", 1000.0, REBALANCE_DATE, "session.duckdb")

    assert stub_optimizer.call_args.args[1] == "GMV"


def test_run_edit_loop_an_unoptimizable_edit_is_reverted_instead_of_ending_the_session(
    monkeypatch, capsys
):
    """Asking MV for a target return no combination of the candidates can
    reach makes PyPortfolioOpt raise. This loop holds live mode's only
    snapshot open, so that must be a rejected edit, not a crash - and the
    next edit must see the reverted target.
    """
    calls: list[float] = []

    def fake_optimizer(
        candidates, objective, value, rebalance_date, db_path, target_annual_return=0.12, risk_free_rate=0.02
    ):
        calls.append(target_annual_return)
        if target_annual_return > 0.5:
            raise ValueError("target_return must be lower than the maximum possible return")
        return _stats(), ({}, 0.0)

    monkeypatch.setattr("src.flow.cli.compute_weights_and_allocation", fake_optimizer)
    monkeypatch.setattr("src.flow.cli.print_weights_and_allocation", lambda *args, **kwargs: None)
    # The refused target, then an unrelated edit that recomputes without
    # naming a target - so the value it carries is whatever survived.
    _script(monkeypatch, "t", "0.99", "o", "GMV", "f")

    _run_edit_loop(["AAPL"], "MV", 1000.0, REBALANCE_DATE, "session.duckdb", target_annual_return=0.10)

    out = capsys.readouterr().out
    assert "Cannot optimize that edit" in out
    assert "Keeping the previous candidates, objective, and target return." in out
    assert calls == [0.99, 0.10]


def test_run_edit_loop_an_unoptimizable_edit_is_never_persisted(monkeypatch, capsys):
    """The pool reaches disk only for an edit that actually produced a
    portfolio - otherwise a reverted edit would outlive the session.
    """
    _fake_ingestion(monkeypatch)
    save_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", save_spy)
    monkeypatch.setattr(
        "src.flow.cli.compute_weights_and_allocation",
        MagicMock(side_effect=ValueError("no solution")),
    )
    monkeypatch.setattr("src.flow.cli.print_weights_and_allocation", lambda *args, **kwargs: None)
    _script(monkeypatch, "a", "NVDA", "f")

    _run_edit_loop(
        ["AAPL"], "GMV", 1000.0, REBALANCE_DATE, "session.duckdb",
        selection="user_provided", memory_path="mem.json",
    )

    assert "Cannot optimize that edit: no solution" in capsys.readouterr().out
    assert save_spy.called is False


# ---------------------------------------------------------------------------
# print_weights_and_allocation
# ---------------------------------------------------------------------------


def test_print_weights_and_allocation_reports_the_figures_behind_the_weights(capsys):
    stats = _stats(
        weights={"SPY": 0.75, "AAPL": 0.25, "NVDA": 0.0},
        expected_returns={"SPY": 0.10, "AAPL": 0.20, "NVDA": 0.40},
        volatility={"SPY": 0.15, "AAPL": 0.25, "NVDA": 0.50},
        portfolio_expected_return=0.125,
        portfolio_volatility=0.16,
        portfolio_sharpe=0.65625,
        target_annual_return=0.125,
    )

    print_weights_and_allocation(stats, ({"SPY": 3, "AAPL": 1}, 12.34), "MV")

    out = capsys.readouterr().out
    assert "  SPY: return=0.1000  volatility=0.1500" in out
    assert "  AAPL: return=0.2000  volatility=0.2500" in out
    # A candidate the optimizer rejected is not reported among the holdings.
    assert "NVDA" not in out
    assert "Portfolio expected return: 0.1250  Portfolio volatility: 0.1600  Portfolio Sharpe: 0.6562" in out
    assert "Risk-free rate used: 0.0200" in out
    assert "Target annual return: 0.1250" in out
    assert "Returns window: 2020-05-01 to 2025-04-01 (60 month(s) of monthly returns)" in out
    assert "Leftover cash: $12.34" in out


def test_run_edit_loop_refuses_a_cross_currency_add_and_does_not_persist(
    monkeypatch, stub_optimizer, capsys
):
    _fake_ingestion(monkeypatch, currencies={"7203.T": "JPY"})
    save_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", save_spy)
    _script(monkeypatch, "a", "AAPL", "f")

    _run_edit_loop(
        ["7203.T"], "GMV", 1000.0, REBALANCE_DATE, "session.duckdb",
        selection="user_provided", memory_path="mem.json", currency="JPY",
    )

    assert "Refused: AAPL is priced in USD but this pool is JPY." in capsys.readouterr().out
    # The pool is unchanged, so whether a (content-identical) write happens is
    # immaterial; what matters is that the refused ticker never reaches disk.
    for call in save_spy.call_args_list:
        assert call.args[0] == ["7203.T"]


def test_run_edit_loop_passes_the_currency_into_every_recompute(monkeypatch, stub_optimizer):
    """The printing needs it on each edit, since the edit loop never goes
    through `print_pipeline_result`.
    """
    printed: list[str] = []
    monkeypatch.setattr(
        "src.flow.cli.print_weights_and_allocation",
        lambda stats, allocation, objective, currency=None, **kwargs: printed.append(currency),
    )
    _fake_ingestion(monkeypatch)
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", MagicMock())
    _script(monkeypatch, "r", "MSFT", "f")

    _run_edit_loop(
        ["AAPL", "MSFT"], "GMV", 1000.0, REBALANCE_DATE, "session.duckdb", currency="JPY"
    )

    assert printed == ["JPY"]


def test_print_weights_and_allocation_labels_money_with_the_portfolio_currency(capsys):
    stats = _stats(weights={"7203.T": 1.0}, expected_returns={"7203.T": 0.1}, volatility={"7203.T": 0.2})

    print_weights_and_allocation(stats, ({"7203.T": 100}, 1204.0), "GMV", "JPY")

    out = capsys.readouterr().out
    assert "Portfolio currency: JPY - --value is interpreted as JPY" in out
    assert "Leftover cash: ¥1,204.00 JPY" in out


def test_print_weights_and_allocation_defaults_to_usd(capsys):
    """The default keeps every pre-currency caller and assertion working:
    "Leftover cash: $12.34" is still a substring of the new output.
    """
    stats = _stats(weights={"SPY": 1.0}, expected_returns={"SPY": 0.1}, volatility={"SPY": 0.15})

    print_weights_and_allocation(stats, ({"SPY": 1}, 12.34), "GMV")

    out = capsys.readouterr().out
    assert "Portfolio currency: USD" in out
    assert "Leftover cash: $12.34" in out


def test_format_money_falls_back_to_the_iso_code_alone():
    from src.flow.cli import format_money

    assert format_money(5.0, "SGD") == "5.00 SGD"
    assert format_money(1234.5, "USD") == "$1,234.50 USD"


def test_print_weights_and_allocation_marks_target_return_not_applicable_for_gmv(capsys):
    """The line is printed for every objective so its presence and position
    never depend on which objective ran.
    """
    stats = _stats(weights={"SPY": 1.0}, expected_returns={"SPY": 0.1}, volatility={"SPY": 0.15})

    print_weights_and_allocation(stats, ({"SPY": 1}, 0.0), "GMV")

    assert "Target annual return: n/a (objective is GMV, not MV)" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# main's --target-return wiring
# ---------------------------------------------------------------------------


def _stub_main_pipeline(monkeypatch) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Stub everything `main` calls after argument parsing, returning the
    `run_pipeline_against`, `_run_edit_loop` and `_settle_benchmark` spies.

    `_settle_benchmark` must be stubbed and not merely tolerated: left real,
    it would resolve USD's default `SPY` and go to yfinance for its history,
    putting a network call inside every one of these tests. `prepare_holdings`
    is stubbed for the same reason and one more: left real it would read the
    developer's own `memory/portfolio.json`, so these tests would pass or
    fetch depending on whose machine they ran on.
    """

    @contextmanager
    def fake_session(rebalance_date, selection, db_path):
        yield "session.duckdb", "backtest"

    pipeline_spy = MagicMock(return_value={"scan_detail": {"candidates": ["AAPL"]}})
    edit_spy = MagicMock()
    benchmark_spy = MagicMock(return_value=_benchmark_source("SPY"))
    monkeypatch.setattr("src.flow.cli.open_pipeline_session", fake_session)
    monkeypatch.setattr("src.flow.cli.run_pipeline_against", pipeline_spy)
    monkeypatch.setattr("src.flow.cli.print_pipeline_result", lambda result: None)
    monkeypatch.setattr("src.flow.cli._run_edit_loop", edit_spy)
    monkeypatch.setattr("src.flow.cli._settle_benchmark", benchmark_spy)
    monkeypatch.setattr(
        "src.flow.cli.prepare_holdings",
        MagicMock(return_value=unavailable_holdings("USD", {}, 0.02, "stubbed")),
    )
    monkeypatch.setattr("src.flow.cli.load_portfolio", MagicMock(return_value={}))
    return pipeline_spy, edit_spy, benchmark_spy


def _stub_main_holdings(monkeypatch, positions=None) -> MagicMock:
    """Replace `main`'s holdings resolution with a spy, so a test can assert
    which currency, database and flags reached it without any I/O.
    """
    holdings_spy = MagicMock(
        return_value=unavailable_holdings("USD", positions or {}, 0.02, "stubbed")
    )
    monkeypatch.setattr("src.flow.cli.prepare_holdings", holdings_spy)
    monkeypatch.setattr("src.flow.cli.load_portfolio", MagicMock(return_value=positions or {}))
    return holdings_spy


def test_main_threads_the_target_return_argument_into_the_pipeline_and_the_edit_loop(monkeypatch):
    pipeline_spy, edit_spy, _benchmark_spy = _stub_main_pipeline(monkeypatch)
    monkeypatch.setattr(
        "sys.argv",
        ["portfolio", "--date", "2024-03-01", "--objective", "MV", "--value", "1000", "--target-return", "0.09"],
    )

    main()

    assert pipeline_spy.call_args.kwargs["target_annual_return"] == 0.09
    assert edit_spy.call_args.kwargs["target_annual_return"] == 0.09


def test_main_target_return_defaults_when_the_argument_is_omitted(monkeypatch):
    pipeline_spy, _edit_spy, _benchmark_spy = _stub_main_pipeline(monkeypatch)
    monkeypatch.setattr(
        "sys.argv", ["portfolio", "--date", "2024-03-01", "--objective", "GMV", "--value", "1000"]
    )

    main()

    assert pipeline_spy.call_args.kwargs["target_annual_return"] == DEFAULT_TARGET_ANNUAL_RETURN


def test_main_threads_the_risk_free_rate_argument_into_the_pipeline_and_the_edit_loop(monkeypatch):
    """The edit loop must receive it too: a rate that applied only to the
    initial run would silently revert to the configured default on the
    first interactive edit.
    """
    pipeline_spy, edit_spy, _benchmark_spy = _stub_main_pipeline(monkeypatch)
    monkeypatch.setattr(
        "sys.argv",
        ["portfolio", "--date", "2024-03-01", "--objective", "MSR", "--value", "1000",
         "--risk-free-rate", "0.015"],
    )

    main()

    assert pipeline_spy.call_args.kwargs["risk_free_rate"] == 0.015
    assert edit_spy.call_args.kwargs["risk_free_rate"] == 0.015


def test_main_risk_free_rate_defaults_to_the_configured_setting(monkeypatch):
    pipeline_spy, _edit_spy, _benchmark_spy = _stub_main_pipeline(monkeypatch)
    monkeypatch.setattr(
        "sys.argv", ["portfolio", "--date", "2024-03-01", "--objective", "MSR", "--value", "1000"]
    )

    main()

    assert pipeline_spy.call_args.kwargs["risk_free_rate"] == settings.risk_free_rate


def test_run_edit_loop_carries_the_risk_free_rate_into_every_recompute(monkeypatch, stub_optimizer):
    """It is not editable in the loop, but it must survive one - otherwise
    --risk-free-rate would apply to the initial run only.
    """
    _fake_ingestion(monkeypatch)
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", MagicMock())
    _script(monkeypatch, "a", "NVDA", "f")

    _run_edit_loop(
        ["AAPL"], "MSR", 1000.0, REBALANCE_DATE, "session.duckdb", risk_free_rate=0.015
    )

    assert stub_optimizer.call_args.kwargs["risk_free_rate"] == 0.015


# ---------------------------------------------------------------------------
# the benchmark: report line, prompt, [b], and main's wiring
# ---------------------------------------------------------------------------


def _benchmark_source(ticker: str | None = "SPY", currency: str = "USD", reason: str | None = None):
    """A `BenchmarkSource` standing in for one already resolved - the return
    history is irrelevant wherever the source is passed through rather than
    measured.
    """
    return BenchmarkSource(
        ticker=ticker,
        currency=currency,
        monthly_returns=pd.Series(dtype=float, index=pd.DatetimeIndex([], name="rebalance_date")),
        unavailable_reason=reason,
    )


def _benchmark_stats(**overrides) -> BenchmarkStats:
    fields = {
        "ticker": "SPY",
        "currency": "USD",
        "annual_return": 0.1225,
        "annual_volatility": 0.1553,
        "sharpe": 0.66,
        "risk_free_rate": 0.02,
        "window_start": date(2020, 5, 1),
        "window_end": date(2025, 4, 1),
        "window_months": 60,
        "unavailable_reason": None,
    }
    return BenchmarkStats(**{**fields, **overrides})


def _fake_benchmark_prepare(monkeypatch, outcomes: dict[str, str | None]) -> MagicMock:
    """Stand in for `prepare_benchmark`: each ticker in `outcomes` maps to the
    refusal reason it should come back with, or `None` for "usable".
    """
    spy = MagicMock(
        side_effect=lambda ticker, currency, rebalance_date, db_path, allow_fetch=True: _benchmark_source(
            ticker, currency, outcomes.get(ticker, f"{ticker} is unknown to this stub")
        )
    )
    monkeypatch.setattr("src.flow.cli.prepare_benchmark", spy)
    return spy


def test_print_weights_and_allocation_reports_the_benchmark_line(capsys):
    stats = _stats(weights={"SPY": 1.0}, expected_returns={"SPY": 0.13}, volatility={"SPY": 0.16})

    print_weights_and_allocation(
        stats, ({"SPY": 1}, 0.0), "GMV", "USD", benchmark=_benchmark_stats()
    )

    out = capsys.readouterr().out
    assert "Benchmark SPY: return=0.1225  volatility=0.1553  Sharpe=0.6600  (60 of 60 month(s))" in out


def test_the_benchmark_line_sits_directly_below_the_portfolios_own_figures(capsys):
    """The adjacency is the point: the comparison only reads as a comparison
    when the two triplets are on consecutive lines.
    """
    print_weights_and_allocation(_stats(), ({}, 0.0), "GMV", "USD", benchmark=_benchmark_stats())

    lines = capsys.readouterr().out.splitlines()
    portfolio_line = next(i for i, line in enumerate(lines) if line.startswith("Portfolio expected return:"))
    assert lines[portfolio_line + 1].startswith("Benchmark SPY:")
    assert lines[portfolio_line + 2].startswith("Risk-free rate used:")


def test_print_weights_and_allocation_notes_a_shorter_benchmark_window(capsys):
    print_weights_and_allocation(
        _stats(), ({}, 0.0), "GMV", "USD", benchmark=_benchmark_stats(window_months=58)
    )

    assert "(58 of 60 month(s))" in capsys.readouterr().out


def test_print_weights_and_allocation_prints_an_unavailable_benchmark_with_its_reason(capsys):
    print_weights_and_allocation(
        _stats(), ({}, 0.0), "GMV", "JPY",
        benchmark=_benchmark_stats(
            annual_return=None, annual_volatility=None, sharpe=None, window_months=0,
            unavailable_reason="SPY trades in USD but this portfolio is JPY",
        ),
    )

    out = capsys.readouterr().out
    assert "Benchmark SPY: n/a - SPY trades in USD but this portfolio is JPY" in out


def test_print_weights_and_allocation_omits_the_benchmark_line_when_there_is_none(capsys):
    """Protects every caller that never asked for a benchmark, and is what
    `--benchmark none` produces.
    """
    print_weights_and_allocation(_stats(), ({}, 0.0), "GMV", "USD")

    assert "Benchmark" not in capsys.readouterr().out


def test_format_benchmark_names_no_ticker_when_none_was_chosen():
    line = format_benchmark(
        _benchmark_stats(
            ticker=None, annual_return=None, annual_volatility=None, sharpe=None, window_months=0,
            unavailable_reason="no benchmark is set for this SGD portfolio",
        ),
        60,
    )

    assert line == "Benchmark: n/a - no benchmark is set for this SGD portfolio"


def test_choose_pool_to_resume_shows_each_pools_benchmark(monkeypatch, capsys):
    _script(monkeypatch, "USD")

    _choose_pool_to_resume(
        {"USD": ["AAPL", "SPY"], "JPY": ["7203.T"]}, {"USD": "SPY", "JPY": None}
    )

    out = capsys.readouterr().out
    assert "USD (2, benchmark SPY): AAPL, SPY" in out
    assert "JPY (1): 7203.T" in out


def test_settle_benchmark_takes_the_currency_default_without_asking(monkeypatch):
    prepare_spy = _fake_benchmark_prepare(monkeypatch, {"SPY": None})
    save_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", save_spy)
    monkeypatch.setattr("src.flow.cli.load_candidate_benchmark", lambda path, currency: None)
    _script(monkeypatch)  # any input() call would raise StopIteration

    source = _settle_benchmark(
        ["AAPL"], "USD", REBALANCE_DATE, "session.duckdb", pool_memory_path="mem.json"
    )

    assert source.ticker == "SPY"
    assert prepare_spy.call_args.args[0] == "SPY"
    assert save_spy.called is False, "a default is never written into the pool file"


def test_settle_benchmark_prefers_what_the_pool_recorded(monkeypatch):
    _fake_benchmark_prepare(monkeypatch, {"VOO": None})
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", MagicMock())
    monkeypatch.setattr("src.flow.cli.load_candidate_benchmark", lambda path, currency: "VOO")

    source = _settle_benchmark(
        ["AAPL"], "USD", REBALANCE_DATE, "session.duckdb", pool_memory_path="mem.json"
    )

    assert source.ticker == "VOO"


def test_settle_benchmark_persists_an_explicit_override(monkeypatch):
    _fake_benchmark_prepare(monkeypatch, {"QQQ": None})
    save_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", save_spy)
    monkeypatch.setattr("src.flow.cli.load_candidate_benchmark", lambda path, currency: None)

    _settle_benchmark(
        ["AAPL"], "USD", REBALANCE_DATE, "session.duckdb",
        override="QQQ", pool_memory_path="mem.json",
    )

    assert save_spy.call_args.kwargs["benchmark"] == "QQQ"
    assert save_spy.call_args.kwargs["currency"] == "USD"


def test_settle_benchmark_does_not_persist_an_override_that_was_refused(monkeypatch):
    _fake_benchmark_prepare(monkeypatch, {"QQQ": "QQQ trades in USD but this portfolio is JPY"})
    save_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", save_spy)
    monkeypatch.setattr("src.flow.cli.load_candidate_benchmark", lambda path, currency: None)

    source = _settle_benchmark(
        ["7203.T"], "JPY", REBALANCE_DATE, "session.duckdb",
        override="QQQ", pool_memory_path="mem.json",
    )

    assert source.unavailable_reason is not None
    assert save_spy.called is False


def test_settle_benchmark_asks_when_the_currency_has_no_default(monkeypatch, capsys):
    _fake_benchmark_prepare(monkeypatch, {"1306.T": None})
    save_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", save_spy)
    monkeypatch.setattr("src.flow.cli.load_candidate_benchmark", lambda path, currency: None)
    _script(monkeypatch, "1306.t")

    source = _settle_benchmark(
        ["7203.T"], "JPY", REBALANCE_DATE, "session.duckdb", pool_memory_path="mem.json"
    )

    out = capsys.readouterr().out
    assert "No benchmark recorded for this JPY pool." in out
    assert "Benchmark set to 1306.T." in out
    assert source.ticker == "1306.T"
    assert save_spy.call_args.kwargs["benchmark"] == "1306.T"


def test_settle_benchmark_refuses_a_wrong_currency_answer_and_asks_again(monkeypatch, capsys):
    _fake_benchmark_prepare(
        monkeypatch,
        {"SPY": "SPY trades in USD but this portfolio is JPY", "1306.T": None},
    )
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", MagicMock())
    monkeypatch.setattr("src.flow.cli.load_candidate_benchmark", lambda path, currency: None)
    _script(monkeypatch, "SPY", "1306.T")

    source = _settle_benchmark(
        ["7203.T"], "JPY", REBALANCE_DATE, "session.duckdb", pool_memory_path="mem.json"
    )

    out = capsys.readouterr().out
    assert "Refused: SPY trades in USD but this portfolio is JPY" in out
    assert source.ticker == "1306.T"


def test_settle_benchmark_skips_on_a_blank_answer(monkeypatch):
    prepare_spy = _fake_benchmark_prepare(monkeypatch, {})
    save_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", save_spy)
    monkeypatch.setattr("src.flow.cli.load_candidate_benchmark", lambda path, currency: None)
    _script(monkeypatch, "")

    _settle_benchmark(
        ["7203.T"], "JPY", REBALANCE_DATE, "session.duckdb", pool_memory_path="mem.json"
    )

    assert prepare_spy.call_args.args[0] is None, "the skip is reported, not silently dropped"
    assert save_spy.called is False


def test_settle_benchmark_never_asks_a_selection_with_no_pool_file(monkeypatch):
    """An agent-chosen candidate list has nowhere to record a benchmark and
    nobody mid-conversation to ask, so it takes the default and moves on.
    """
    prepare_spy = _fake_benchmark_prepare(monkeypatch, {"SPY": None})
    _script(monkeypatch)

    source = _settle_benchmark(["AAPL"], "USD", REBALANCE_DATE, "session.duckdb")

    assert source.ticker == "SPY"
    assert prepare_spy.call_count == 1


def test_settle_benchmark_disabled_returns_nothing(monkeypatch):
    prepare_spy = _fake_benchmark_prepare(monkeypatch, {"SPY": None})

    assert _settle_benchmark(["AAPL"], "USD", REBALANCE_DATE, "session.duckdb", enabled=False) is None
    assert prepare_spy.called is False


def test_settle_benchmark_threads_allow_fetch_through(monkeypatch):
    prepare_spy = _fake_benchmark_prepare(monkeypatch, {"SPY": None})

    _settle_benchmark(["AAPL"], "USD", REBALANCE_DATE, "session.duckdb", allow_fetch=False)

    assert prepare_spy.call_args.kwargs["allow_fetch"] is False


def test_run_edit_loop_reprints_the_benchmark_after_every_edit(monkeypatch, stub_optimizer):
    """The comparison has to follow each edit, not describe the run as it was
    before it - the same reason the returns-window line lives in
    `print_weights_and_allocation` rather than the header.
    """
    printed: list[object] = []
    monkeypatch.setattr(
        "src.flow.cli.print_weights_and_allocation",
        lambda *args, benchmark=None, **kwargs: printed.append(benchmark),
    )
    _fake_ingestion(monkeypatch)
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", MagicMock())
    _script(monkeypatch, "a", "NVDA", "r", "NVDA", "f")

    _run_edit_loop(
        ["AAPL"], "GMV", 1000.0, REBALANCE_DATE, "session.duckdb",
        benchmark=_benchmark_source("SPY"),
    )

    assert len(printed) == 2
    assert all(b is not None and b.ticker == "SPY" for b in printed)


def test_run_edit_loop_can_change_the_benchmark(monkeypatch, stub_optimizer, capsys):
    printed: list[object] = []
    monkeypatch.setattr(
        "src.flow.cli.print_weights_and_allocation",
        lambda *args, benchmark=None, **kwargs: printed.append(benchmark),
    )
    _fake_benchmark_prepare(monkeypatch, {"QQQ": None})
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", MagicMock())
    _script(monkeypatch, "b", "QQQ", "f")

    _run_edit_loop(
        ["AAPL"], "GMV", 1000.0, REBALANCE_DATE, "session.duckdb",
        benchmark=_benchmark_source("SPY"),
    )

    assert "Benchmark set to QQQ." in capsys.readouterr().out
    assert [b.ticker for b in printed] == ["QQQ"]


def test_run_edit_loop_keeps_the_previous_benchmark_when_a_new_one_fails(
    monkeypatch, stub_optimizer, capsys
):
    """The same keep-what-you-had treatment `[o]bjective` gives a rejected
    edit - and no recompute, since nothing changed.
    """
    printed: list[object] = []
    monkeypatch.setattr(
        "src.flow.cli.print_weights_and_allocation",
        lambda *args, benchmark=None, **kwargs: printed.append(benchmark),
    )
    _fake_benchmark_prepare(monkeypatch, {"ZZZZ": "no price data found"})
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", MagicMock())
    _script(monkeypatch, "b", "ZZZZ", "", "f")

    _run_edit_loop(
        ["AAPL"], "GMV", 1000.0, REBALANCE_DATE, "session.duckdb",
        benchmark=_benchmark_source("SPY"),
    )

    assert "Refused: no price data found" in capsys.readouterr().out
    assert printed == []


def test_run_edit_loop_persists_a_changed_benchmark_for_user_provided(monkeypatch, stub_optimizer):
    _fake_benchmark_prepare(monkeypatch, {"QQQ": None})
    save_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", save_spy)
    _script(monkeypatch, "b", "QQQ", "f")

    _run_edit_loop(
        ["AAPL"], "GMV", 1000.0, REBALANCE_DATE, "session.duckdb",
        selection="user_provided", memory_path="mem.json",
        benchmark=_benchmark_source("SPY"),
    )

    assert save_spy.call_args.kwargs["benchmark"] == "QQQ"
    assert save_spy.call_args.kwargs["path"] == "mem.json"


def test_run_edit_loop_non_user_provided_never_persists_a_benchmark(monkeypatch, stub_optimizer):
    _fake_benchmark_prepare(monkeypatch, {"QQQ": None})
    save_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", save_spy)
    _script(monkeypatch, "b", "QQQ", "f")

    _run_edit_loop(
        ["AAPL"], "GMV", 1000.0, REBALANCE_DATE, "session.duckdb",
        selection="llm_s_only", benchmark=_benchmark_source("SPY"),
    )

    assert save_spy.called is False


def test_main_threads_the_benchmark_into_the_pipeline_and_the_edit_loop(monkeypatch):
    pipeline_spy, edit_spy, benchmark_spy = _stub_main_pipeline(monkeypatch)
    monkeypatch.setattr(
        "sys.argv",
        ["portfolio", "--date", "2024-03-01", "--objective", "GMV", "--value", "1000",
         "--benchmark", "QQQ"],
    )

    main()

    assert benchmark_spy.call_args.kwargs["override"] == "QQQ"
    assert benchmark_spy.call_args.kwargs["enabled"] is True
    assert pipeline_spy.call_args.kwargs["benchmark"] is benchmark_spy.return_value
    assert edit_spy.call_args.kwargs["benchmark"] is benchmark_spy.return_value


def test_main_defaults_the_benchmark_override_to_nothing(monkeypatch):
    """Left out, the flag overrides nothing - the pool's own record and then
    the currency's default decide.
    """
    _pipeline_spy, _edit_spy, benchmark_spy = _stub_main_pipeline(monkeypatch)
    monkeypatch.setattr(
        "sys.argv", ["portfolio", "--date", "2024-03-01", "--objective", "GMV", "--value", "1000"]
    )

    main()

    assert benchmark_spy.call_args.kwargs["override"] is None
    assert benchmark_spy.call_args.kwargs["enabled"] is True


def test_main_benchmark_none_disables_the_comparison(monkeypatch):
    pipeline_spy, edit_spy, benchmark_spy = _stub_main_pipeline(monkeypatch)
    benchmark_spy.return_value = None
    monkeypatch.setattr(
        "sys.argv",
        ["portfolio", "--date", "2024-03-01", "--objective", "GMV", "--value", "1000",
         "--benchmark", "none"],
    )

    main()

    assert benchmark_spy.call_args.kwargs["enabled"] is False
    assert benchmark_spy.call_args.kwargs["override"] is None
    assert pipeline_spy.call_args.kwargs["benchmark"] is None
    assert edit_spy.call_args.kwargs["benchmark"] is None


def test_main_no_benchmark_fetch_is_threaded_through(monkeypatch):
    _pipeline_spy, edit_spy, benchmark_spy = _stub_main_pipeline(monkeypatch)
    monkeypatch.setattr(
        "sys.argv",
        ["portfolio", "--date", "2024-03-01", "--objective", "GMV", "--value", "1000",
         "--no-benchmark-fetch"],
    )

    main()

    assert benchmark_spy.call_args.kwargs["allow_fetch"] is False
    assert edit_spy.call_args.kwargs["allow_benchmark_fetch"] is False


def test_main_only_offers_pool_memory_to_the_user_provided_selection(monkeypatch):
    """An agent-chosen list must not be able to write a benchmark into a pool
    file it does not own.
    """
    _pipeline_spy, _edit_spy, benchmark_spy = _stub_main_pipeline(monkeypatch)
    monkeypatch.setattr(
        "sys.argv",
        ["portfolio", "--date", "2024-03-01", "--objective", "GMV", "--value", "1000",
         "--selection", "llm_s_only"],
    )

    main()

    assert benchmark_spy.call_args.kwargs["pool_memory_path"] is None


# ---------------------------------------------------------------------------
# main's holdings block
# ---------------------------------------------------------------------------


def test_main_reports_the_saved_portfolio_for_the_runs_own_currency(monkeypatch):
    _stub_main_pipeline(monkeypatch)
    holdings_spy = _stub_main_holdings(monkeypatch, {"SPY": 1000.0})
    monkeypatch.setattr(
        "sys.argv",
        ["portfolio", "--date", "2024-03-01", "--objective", "GMV", "--value", "1000"],
    )

    main()

    assert holdings_spy.call_args.args[0] == {"SPY": 1000.0}
    assert holdings_spy.call_args.args[1] == "USD"


def test_main_measures_the_holdings_against_the_same_risk_free_rate_as_the_pool(monkeypatch):
    """The whole point of the block is reading it beside the pool's and the
    benchmark's figures, which only works if all three use one rate.
    """
    _stub_main_pipeline(monkeypatch)
    holdings_spy = _stub_main_holdings(monkeypatch, {"SPY": 1000.0})
    monkeypatch.setattr(
        "sys.argv",
        ["portfolio", "--date", "2024-03-01", "--objective", "GMV", "--value", "1000",
         "--risk-free-rate", "0.045"],
    )

    main()

    assert holdings_spy.call_args.kwargs["risk_free_rate"] == 0.045


def test_main_no_holdings_leaves_the_block_out_entirely(monkeypatch):
    _stub_main_pipeline(monkeypatch)
    holdings_spy = _stub_main_holdings(monkeypatch, {"SPY": 1000.0})
    monkeypatch.setattr(
        "sys.argv",
        ["portfolio", "--date", "2024-03-01", "--objective", "GMV", "--value", "1000",
         "--no-holdings"],
    )

    main()

    assert holdings_spy.called is False


def test_main_no_holdings_fetch_is_threaded_through(monkeypatch):
    _stub_main_pipeline(monkeypatch)
    holdings_spy = _stub_main_holdings(monkeypatch, {"SPY": 1000.0})
    monkeypatch.setattr(
        "sys.argv",
        ["portfolio", "--date", "2024-03-01", "--objective", "GMV", "--value", "1000",
         "--no-holdings-fetch"],
    )

    main()

    assert holdings_spy.call_args.kwargs["allow_fetch"] is False


def test_main_measures_the_holdings_against_the_session_database_not_the_shared_cache(monkeypatch):
    """`--db-path` is the shared S&P 500 cache; the session database is what
    `open_pipeline_session` yielded. The holdings must be resolved against
    the latter, for the same reason the benchmark is.
    """
    _stub_main_pipeline(monkeypatch)
    holdings_spy = _stub_main_holdings(monkeypatch, {"SPY": 1000.0})
    monkeypatch.setattr(
        "sys.argv",
        ["portfolio", "--date", "2024-03-01", "--objective", "GMV", "--value", "1000",
         "--db-path", "data/portfolio.duckdb"],
    )

    main()

    assert holdings_spy.call_args.args[3] == "session.duckdb"
