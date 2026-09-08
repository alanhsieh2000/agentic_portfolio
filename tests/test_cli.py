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

import contextlib
import json
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.flow.cli import (
    _choose_pool_to_resume,
    _resolve_mixed_persisted_pool,
    _run_edit_loop,
    _run_user_provided_confirm_loop,
    _settle_benchmark,
    _settle_dividend_floor,
    format_benchmark,
    format_split_restatement,
    format_stale_share_counts,
    main,
    print_weights_and_allocation,
)
from src.config.settings import settings
from src.optimizer.benchmark import BenchmarkSource, BenchmarkStats
from src.optimizer.dividends import DividendFloor, DividendFloorError
from src.flow.backtest import compute_sharpe_ratio
from src.flow.rate_memory import (
    DEFAULT_RATES_PATH,
    load_all_risk_free_rates,
    load_risk_free_rate,
    save_risk_free_rate,
)
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



def _render(stats, allocation=None, objective="GMV", currency="USD", portfolio_value=None) -> str:
    """`print_weights_and_allocation` captured as a string, so a test can
    assert on the lines it printed.
    """
    import io
    import contextlib

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        print_weights_and_allocation(
            stats,
            allocation if allocation is not None else ({}, 0.0),
            objective,
            currency,
            portfolio_value=portfolio_value,
        )
    return buffer.getvalue()

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
        candidates, objective, value, rebalance_date, db_path, target_annual_return=0.12,
        risk_free_rate=0.02, dividend_floor=None, **_kwargs,
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
    assert "Keeping the previous candidates, objective, target return, and dividend floor." in out
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
    def fake_session(rebalance_date, selection, db_path, **_kwargs):
        yield "session.duckdb", "backtest"

    pipeline_spy = MagicMock(return_value={"scan_detail": {"candidates": ["AAPL"]}})
    edit_spy = MagicMock()
    benchmark_spy = MagicMock(return_value=_benchmark_source("SPY"))
    monkeypatch.setattr("src.flow.cli.open_pipeline_session", fake_session)
    monkeypatch.setattr("src.flow.cli.run_pipeline_against", pipeline_spy)
    monkeypatch.setattr("src.flow.cli.print_pipeline_result", lambda result, **kwargs: None)
    monkeypatch.setattr("src.flow.cli._run_edit_loop", edit_spy)
    monkeypatch.setattr("src.flow.cli._settle_benchmark", benchmark_spy)
    monkeypatch.setattr(
        "src.flow.cli.prepare_holdings",
        MagicMock(return_value=unavailable_holdings("USD", {}, 0.02, "stubbed")),
    )
    monkeypatch.setattr("src.flow.cli.load_portfolio", MagicMock(return_value={}))
    # The rate memory is stubbed for the same reason `load_portfolio` is: left
    # real, these tests would read - and, for any run passing
    # `--risk-free-rate`, WRITE - the developer's own `memory/rates.json`,
    # passing on a clean checkout and failing on a machine that has ever
    # remembered a rate. The tests that exercise the file itself use
    # `_rates_argv` instead, which points a real path into `tmp_path`.
    monkeypatch.setattr("src.flow.cli.load_risk_free_rate", MagicMock(return_value=None))
    monkeypatch.setattr("src.flow.cli.save_risk_free_rate", MagicMock(return_value=True))
    # And `load_all_pools`, for the third time the same reason: a
    # `--selection user_provided` test would otherwise read the developer's
    # own `memory/candidates.json` and be shown - or asked about - whatever
    # pools happen to be saved there. The tests that exercise the pool
    # listing itself pass their own dict to the confirm loop directly.
    monkeypatch.setattr("src.flow.cli.load_all_pools", MagicMock(return_value={}))
    return pipeline_spy, edit_spy, benchmark_spy


def _stub_main_confirm_loop(monkeypatch, pool=None, currency="USD") -> MagicMock:
    """Replace `_run_user_provided_confirm_loop` with a spy, so a
    `--selection user_provided` test can assert what reached it without
    driving the interactive add/remove loop or touching Yahoo Finance.
    """
    confirm_spy = MagicMock(return_value=(pool or ["AAPL"], currency))
    monkeypatch.setattr("src.flow.cli._run_user_provided_confirm_loop", confirm_spy)
    return confirm_spy


def _rates_argv(tmp_path, *extra: str) -> list[str]:
    """A minimal `portfolio` argv with `--rates-path` pointed inside
    `tmp_path`, for the tests that exercise the real rate memory.

    The flag is required rather than optional: an argparse default is bound
    at parse time, so monkeypatching `DEFAULT_RATES_PATH` would not take
    effect.
    """
    return [
        "portfolio", "--date", "2024-03-01", "--objective", "GMV", "--value", "1000",
        "--rates-path", str(tmp_path / "rates.json"), *extra,
    ]


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


# ---------------------------------------------------------------------------
# main's per-currency risk-free rate
# ---------------------------------------------------------------------------


def test_main_threads_one_resolved_rate_into_all_three_report_blocks(monkeypatch, tmp_path):
    """The pool's figures, the benchmark's and the holdings' are printed one
    under another so they can be compared; that is only legitimate if a
    single resolved float reaches all three. `run_pipeline_against` feeds the
    benchmark internally, so asserting on it covers the middle block.
    """
    pipeline_spy, edit_spy, _benchmark_spy = _stub_main_pipeline(monkeypatch)
    holdings_spy = _stub_main_holdings(monkeypatch, {"SPY": 1000.0})
    monkeypatch.setattr("sys.argv", _rates_argv(tmp_path, "--risk-free-rate", "0.045"))

    main()

    assert pipeline_spy.call_args.kwargs["risk_free_rate"] == 0.045
    assert holdings_spy.call_args.kwargs["risk_free_rate"] == 0.045
    assert edit_spy.call_args.kwargs["risk_free_rate"] == 0.045


def test_main_remembers_a_rate_given_on_the_command_line(monkeypatch, tmp_path, capsys):
    _stub_main_pipeline(monkeypatch)
    monkeypatch.setattr("src.flow.cli.load_risk_free_rate", load_risk_free_rate)
    monkeypatch.setattr("src.flow.cli.save_risk_free_rate", save_risk_free_rate)
    monkeypatch.setattr("sys.argv", _rates_argv(tmp_path, "--risk-free-rate", "0.045"))

    main()

    assert load_all_risk_free_rates(str(tmp_path / "rates.json")) == {"USD": 0.045}
    assert "Remembered 0.0450 as the USD risk-free rate" in capsys.readouterr().out


def test_main_writes_the_rate_to_the_given_rates_path_and_not_the_default(monkeypatch, tmp_path):
    """The guard against a test suite that edits the developer's own memory.

    Asserted by spying on the PATH `save_risk_free_rate` was handed, not by
    inspecting `DEFAULT_RATES_PATH`'s contents. An earlier version of this
    test did the latter - "the real file has no USD entry" - which passed on
    a clean checkout and then failed the moment somebody actually remembered
    a USD rate, since `memory/` is untracked. A test that asserts something
    about a developer's own data is the very coupling this test exists to
    prevent, so it must not do it either.
    """
    _stub_main_pipeline(monkeypatch)
    monkeypatch.setattr("src.flow.cli.load_risk_free_rate", load_risk_free_rate)
    save_spy = MagicMock(return_value=True)
    monkeypatch.setattr("src.flow.cli.save_risk_free_rate", save_spy)
    monkeypatch.setattr("sys.argv", _rates_argv(tmp_path, "--risk-free-rate", "0.045"))

    main()

    assert save_spy.call_args.kwargs["path"] == str(tmp_path / "rates.json")
    assert save_spy.call_args.kwargs["path"] != DEFAULT_RATES_PATH


def test_main_applies_a_remembered_rate_when_the_flag_is_left_out(monkeypatch, tmp_path):
    pipeline_spy, _edit_spy, _benchmark_spy = _stub_main_pipeline(monkeypatch)
    monkeypatch.setattr("src.flow.cli.load_risk_free_rate", load_risk_free_rate)
    save_risk_free_rate(0.0425, path=str(tmp_path / "rates.json"), currency="USD")
    monkeypatch.setattr("sys.argv", _rates_argv(tmp_path))

    main()

    assert pipeline_spy.call_args.kwargs["risk_free_rate"] == 0.0425


def test_main_does_not_rewrite_a_rate_it_merely_inherited(monkeypatch, tmp_path):
    """Only an explicit choice is recorded, so a run that used a remembered
    rate must not restamp its `updated_at`.
    """
    _stub_main_pipeline(monkeypatch)
    monkeypatch.setattr("src.flow.cli.load_risk_free_rate", load_risk_free_rate)
    save_spy = MagicMock(return_value=True)
    monkeypatch.setattr("src.flow.cli.save_risk_free_rate", save_spy)
    save_risk_free_rate(0.0425, path=str(tmp_path / "rates.json"), currency="USD")
    monkeypatch.setattr("sys.argv", _rates_argv(tmp_path))

    main()

    assert save_spy.called is False


def test_main_refuses_a_nan_rate_before_opening_a_session(monkeypatch, tmp_path, capsys):
    """A live session builds a real snapshot, so a mistyped rate has to be
    refused before that cost is paid, not after.
    """
    session_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.open_pipeline_session", session_spy)
    monkeypatch.setattr("sys.argv", _rates_argv(tmp_path, "--risk-free-rate", "nan"))

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 2
    assert "finite" in capsys.readouterr().err
    assert session_spy.called is False
    assert not (tmp_path / "rates.json").exists()


def test_main_refuses_a_mistyped_percentage_rate_before_opening_a_session(
    monkeypatch, tmp_path, capsys
):
    session_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.open_pipeline_session", session_spy)
    monkeypatch.setattr("sys.argv", _rates_argv(tmp_path, "--risk-free-rate", "4.5"))

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 2
    assert "4.5% is 0.045" in capsys.readouterr().err
    assert session_spy.called is False


def test_main_does_not_remember_a_rate_when_the_run_never_produced_a_report(monkeypatch, tmp_path):
    """A rate that governed no output must not outlive the run - the same
    ordering `test_run_edit_loop_an_unoptimizable_edit_is_never_persisted`
    pins for the candidate pool.
    """
    _pipeline_spy, _edit_spy, _benchmark_spy = _stub_main_pipeline(monkeypatch)
    monkeypatch.setattr("src.flow.cli.load_risk_free_rate", load_risk_free_rate)
    monkeypatch.setattr("src.flow.cli.save_risk_free_rate", save_risk_free_rate)
    monkeypatch.setattr(
        "src.flow.cli.run_pipeline_against",
        MagicMock(side_effect=ValueError("at least one of the assets must have an expected return")),
    )
    monkeypatch.setattr("sys.argv", _rates_argv(tmp_path, "--risk-free-rate", "0.05"))

    with pytest.raises(ValueError):
        main()

    assert not (tmp_path / "rates.json").exists()


def test_main_reports_which_source_the_rate_came_from(monkeypatch, tmp_path):
    print_spy = MagicMock()
    _stub_main_pipeline(monkeypatch)
    monkeypatch.setattr("src.flow.cli.print_pipeline_result", print_spy)
    monkeypatch.setattr("src.flow.cli.load_risk_free_rate", load_risk_free_rate)
    save_risk_free_rate(0.005, path=str(tmp_path / "rates.json"), currency="USD")
    monkeypatch.setattr("sys.argv", _rates_argv(tmp_path))

    main()

    assert print_spy.call_args.kwargs["risk_free_rate_origin"] == "remembered for USD"


def test_main_carries_the_rates_origin_into_the_edit_loop(monkeypatch, tmp_path):
    """Provenance that appeared on the initial report and vanished on the
    first edit would be worse than none - the same bug plan 10 fixed for the
    rate itself.
    """
    _pipeline_spy, edit_spy, _benchmark_spy = _stub_main_pipeline(monkeypatch)
    monkeypatch.setattr("sys.argv", _rates_argv(tmp_path, "--risk-free-rate", "0.045"))

    main()

    assert edit_spy.call_args.kwargs["risk_free_rate_origin"] == (
        "--risk-free-rate, remembered for USD"
    )


def test_run_edit_loop_reprints_the_rates_origin_on_every_recompute(monkeypatch, stub_optimizer):
    print_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.print_weights_and_allocation", print_spy)
    _script(monkeypatch, "a", "NVDA", "f")

    _run_edit_loop(
        ["AAPL"], "GMV", 1000.0, REBALANCE_DATE, "session.duckdb",
        risk_free_rate_origin="remembered for USD",
    )

    assert print_spy.call_args.kwargs["risk_free_rate_origin"] == "remembered for USD"


def test_a_remembered_rate_never_reaches_the_backtest_sharpe_ratio(tmp_path):
    """`src/flow/backtest.py` produces the figure this project compares
    against the paper's published 0.6324 baseline. A per-currency rate must
    not move it.
    """
    save_risk_free_rate(0.0425, path=str(tmp_path / "rates.json"), currency="USD")
    net_returns = pd.Series([0.01, -0.005, 0.02, 0.0, 0.015])

    expected = (
        (net_returns.mean() - settings.risk_free_rate / 12) / net_returns.std() * (12**0.5)
    )
    assert compute_sharpe_ratio(net_returns) == pytest.approx(expected)


def test_main_names_the_rates_source_in_the_holdings_block_too(monkeypatch, tmp_path):
    """Caught in a live run, not by a test: the origin was threaded into the
    pool block and the edit loop but not into this one, so the holdings block
    printed a bare `Risk-free rate used: 0.0050` - a plausible-looking number
    with no provenance, which is the very failure the provenance line exists
    to prevent. Nothing failed, because the parameter defaults to None.
    """
    _stub_main_pipeline(monkeypatch)
    _stub_main_holdings(monkeypatch, {"SPY": 1000.0})
    print_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.print_user_portfolio", print_spy)
    monkeypatch.setattr("sys.argv", _rates_argv(tmp_path, "--risk-free-rate", "0.045"))

    main()

    assert print_spy.call_args.kwargs["risk_free_rate_origin"] == (
        "--risk-free-rate, remembered for USD"
    )


# ---------------------------------------------------------------------------
# --currency: answering the pool-resume prompt from the command line
# ---------------------------------------------------------------------------


def test_choose_pool_to_resume_with_an_override_resumes_without_prompting(monkeypatch, capsys):
    """`_script` with no responses makes any `input()` raise StopIteration,
    which is how this file asserts "did not prompt".
    """
    _script(monkeypatch)

    tickers, currency = _choose_pool_to_resume(
        {"USD": ["AAPL", "SPY"], "JPY": ["7203.T"]}, override="JPY"
    )

    assert (tickers, currency) == (["7203.T"], "JPY")
    assert "Resuming the JPY pool (--currency)." in capsys.readouterr().out


def test_choose_pool_to_resume_with_an_override_still_lists_what_is_saved(monkeypatch, capsys):
    """The listing is what makes a mistyped currency discoverable rather than
    silent - it shows JPY exists directly above the line saying JYP does not.
    """
    _script(monkeypatch)

    _choose_pool_to_resume({"USD": ["AAPL"], "JPY": ["7203.T"]}, override="JYP")
    out = capsys.readouterr().out

    assert "Saved candidate pools:" in out
    assert "JPY (1): 7203.T" in out
    assert "No saved JYP pool; starting a new one (--currency)." in out


def test_an_override_for_an_unsaved_currency_starts_empty_under_that_currency(monkeypatch):
    """Returning the currency rather than `None` is the whole point: `None`
    would let the first typed ticker establish the pool's currency, so a
    scripted `--currency JPY` that typo'd a dollar ticker would silently
    become a dollar pool instead of refusing it.
    """
    _script(monkeypatch)

    assert _choose_pool_to_resume({"USD": ["AAPL"]}, override="EUR") == ([], "EUR")


def test_an_override_pre_establishes_the_currency_when_nothing_is_saved(monkeypatch):
    _script(monkeypatch)

    assert _choose_pool_to_resume({}, override="JPY") == ([], "JPY")


def test_an_override_is_normalized_to_upper_case(monkeypatch):
    _script(monkeypatch)

    tickers, currency = _choose_pool_to_resume({"JPY": ["7203.T"]}, override=" jpy ")

    assert (tickers, currency) == (["7203.T"], "JPY")


def test_no_override_leaves_the_prompt_exactly_as_it_was(monkeypatch):
    """The flag is additive: the interactive path must be untouched."""
    _script(monkeypatch, "JPY")

    assert _choose_pool_to_resume({"USD": ["AAPL"], "JPY": ["7203.T"]}) == (["7203.T"], "JPY")


def test_confirm_loop_with_an_override_refuses_a_foreign_ticker_on_the_first_add(
    monkeypatch, capsys
):
    """With the currency settled up front there is no "first ticker decides"
    window, so a dollar ticker typed into a JPY session is refused
    immediately rather than establishing a dollar pool.
    """
    _fake_ingestion(monkeypatch, currencies={"AAPL": "USD"})
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", MagicMock())
    _script(monkeypatch, "a", "AAPL", "d")

    with pytest.raises(StopIteration):
        # The pool never becomes non-empty, so `[d]one` is refused and the
        # loop asks again - which exhausts the script.
        _run_user_provided_confirm_loop(
            {}, REBALANCE_DATE, "session.duckdb", memory_path="mem.json", currency="JPY"
        )

    out = capsys.readouterr().out
    assert "Refused: AAPL is priced in USD but this pool is JPY." in out


def test_confirm_loop_with_an_override_saves_under_that_currency(monkeypatch):
    _fake_ingestion(monkeypatch, currencies={"7203.T": "JPY"})
    save_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", save_spy)
    _script(monkeypatch, "a", "7203.T", "d")

    pool, currency = _run_user_provided_confirm_loop(
        {}, REBALANCE_DATE, "session.duckdb", memory_path="mem.json", currency="JPY"
    )

    assert (pool, currency) == (["7203.T"], "JPY")
    assert save_spy.call_args.kwargs["currency"] == "JPY"


def test_confirm_loop_with_an_override_resumes_that_pool_without_prompting(monkeypatch):
    _fake_ingestion(monkeypatch, currencies={"7203.T": "JPY"})
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", MagicMock())
    monkeypatch.setattr("src.flow.cli.load_pool_benchmarks", MagicMock(return_value={}))
    _script(monkeypatch, "d")  # only the [d]one answer; no resume prompt to answer

    pool, currency = _run_user_provided_confirm_loop(
        {"USD": ["AAPL"], "JPY": ["7203.T"]},
        REBALANCE_DATE,
        "session.duckdb",
        memory_path="mem.json",
        currency="JPY",
    )

    assert (pool, currency) == (["7203.T"], "JPY")


# --- the mixed-pool repair prompt, which --currency must also answer -------


def test_a_mixed_pool_with_an_override_keeps_that_group_without_prompting(monkeypatch, capsys):
    """The second currency prompt on this path. A flag that silenced only the
    first would still leave a scripted run waiting here.
    """
    _fake_ingestion(monkeypatch, currencies={"AAPL": "USD", "7203.T": "JPY"})
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", MagicMock())
    monkeypatch.setattr("src.flow.cli.load_pool_benchmarks", MagicMock(return_value={}))
    _script(monkeypatch, "d")

    pool, currency = _run_user_provided_confirm_loop(
        {"JPY": ["AAPL", "7203.T"]},
        REBALANCE_DATE,
        "session.duckdb",
        memory_path="mem.json",
        currency="JPY",
    )

    assert (pool, currency) == (["7203.T"], "JPY")
    out = capsys.readouterr().out
    assert "mixes currencies" in out
    # The data-loss record must survive the override - skipping the question
    # is not a licence to hide the answer.
    assert "Keeping JPY; dropping AAPL (--currency)." in out


def test_a_mixed_pool_whose_groups_lack_the_override_starts_empty_under_it(monkeypatch, capsys):
    _script(monkeypatch)

    tickers, currency = _resolve_mixed_persisted_pool(
        ["AAPL", "SPY"], {"AAPL": "USD", "SPY": "GBP"}, override="JPY"
    )

    assert (tickers, currency) == ([], "JPY")
    assert "No JPY tickers in the saved pool" in capsys.readouterr().out


def test_a_mixed_pool_without_an_override_still_prompts(monkeypatch, capsys):
    _script(monkeypatch, "JPY")

    tickers, currency = _resolve_mixed_persisted_pool(
        ["AAPL", "7203.T"], {"AAPL": "USD", "7203.T": "JPY"}
    )

    assert (tickers, currency) == (["7203.T"], "JPY")
    assert "Keeping JPY; dropping AAPL." in capsys.readouterr().out


def test_a_resumed_pool_that_now_prices_elsewhere_reports_the_disagreement(monkeypatch, capsys):
    """The one thing --currency does not override: what the tickers actually
    price in per the database. A pool saved under USD whose ticker moved to
    Tokyo really is JPY now - but the override losing must not be silent.
    """
    _fake_ingestion(monkeypatch, currencies={"7203.T": "JPY"})
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", MagicMock())
    monkeypatch.setattr("src.flow.cli.load_pool_benchmarks", MagicMock(return_value={}))
    _script(monkeypatch, "d")

    pool, currency = _run_user_provided_confirm_loop(
        {"USD": ["7203.T"]},
        REBALANCE_DATE,
        "session.duckdb",
        memory_path="mem.json",
        currency="USD",
    )

    assert (pool, currency) == (["7203.T"], "JPY")
    assert "the saved USD pool's tickers now price in JPY; continuing as JPY." in (
        capsys.readouterr().out
    )


# --- through main ----------------------------------------------------------


def test_main_threads_the_currency_override_into_the_confirm_loop(monkeypatch, tmp_path):
    _stub_main_pipeline(monkeypatch)
    confirm_spy = _stub_main_confirm_loop(monkeypatch, pool=["7203.T"], currency="JPY")
    monkeypatch.setattr(
        "sys.argv",
        _rates_argv(tmp_path, "--selection", "user_provided", "--currency", "jpy"),
    )

    main()

    assert confirm_spy.call_args.kwargs["currency"] == "JPY"


def test_main_carries_the_overridden_currency_through_the_whole_run(monkeypatch, tmp_path):
    """The flag has to reach more than the pool choice: the report, the
    benchmark, the remembered rate and the holdings block are all keyed off
    the currency the confirm loop returns.
    """
    pipeline_spy, edit_spy, benchmark_spy = _stub_main_pipeline(monkeypatch)
    _stub_main_confirm_loop(monkeypatch, pool=["7203.T"], currency="JPY")
    holdings_spy = _stub_main_holdings(monkeypatch, {"1321.T": 50.0})
    monkeypatch.setattr(
        "sys.argv",
        _rates_argv(tmp_path, "--selection", "user_provided", "--currency", "JPY"),
    )

    main()

    assert pipeline_spy.call_args.kwargs["currency"] == "JPY"
    assert edit_spy.call_args.kwargs["currency"] == "JPY"
    assert benchmark_spy.call_args.args[1] == "JPY"
    assert holdings_spy.call_args.args[1] == "JPY"


def test_main_leaves_the_confirm_loop_unaffected_without_the_flag(monkeypatch, tmp_path):
    _stub_main_pipeline(monkeypatch)
    confirm_spy = _stub_main_confirm_loop(monkeypatch)
    monkeypatch.setattr("sys.argv", _rates_argv(tmp_path, "--selection", "user_provided"))

    main()

    assert confirm_spy.call_args.kwargs["currency"] is None


def test_main_refuses_a_currency_for_an_agent_driven_selection(monkeypatch, tmp_path, capsys):
    """Those selections screen the S&P 500 and are USD by construction, so
    accepting the flag would print a USD report to somebody who asked for
    JPY - the silent contradiction this codebase refuses everywhere else.
    """
    session_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.open_pipeline_session", session_spy)
    monkeypatch.setattr(
        "sys.argv",
        _rates_argv(tmp_path, "--selection", "llm_s_only", "--currency", "JPY"),
    )

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 2
    assert "--currency applies only to --selection user_provided" in capsys.readouterr().err
    assert session_spy.called is False


def test_an_add_that_refuses_everything_does_not_forget_the_override(monkeypatch, capsys):
    """`validate_and_edit_candidates` reports no currency when the resulting
    pool is empty, so a fully-refused add used to clobber the one in force -
    reporting "this pool is None" and then letting the NEXT add establish a
    different currency, quietly undoing `--currency`. Reachable only once a
    currency can be settled before the pool is non-empty.
    """
    _fake_ingestion(monkeypatch, currencies={"AAPL": "USD", "SPY": "USD"})
    monkeypatch.setattr("src.flow.cli.save_candidate_pool", MagicMock())
    _script(monkeypatch, "a", "AAPL", "a", "SPY", "d")

    with pytest.raises(StopIteration):
        _run_user_provided_confirm_loop(
            {}, REBALANCE_DATE, "session.duckdb", memory_path="mem.json", currency="JPY"
        )

    out = capsys.readouterr().out
    # Both adds refused against JPY - the second proves the currency survived
    # the first, rather than SPY establishing a USD pool.
    assert out.count("but this pool is JPY.") == 2
    assert "this pool is None" not in out


# ==========================================================================
# Dividend reporting and the [d]ividend edit-loop option
# ==========================================================================


def _dividend_stats(**overrides) -> PortfolioStats:
    """A `PortfolioStats` carrying dividend figures for a two-ticker pool,
    with T the payer at 3.97% and GROWTH a confirmed non-payer.
    """
    fields = {
        "weights": {"T": 0.7, "GROWTH": 0.3},
        "expected_returns": {"T": 0.08, "GROWTH": 0.19},
        "volatility": {"T": 0.20, "GROWTH": 0.26},
        "dividend_yields": {"T": 0.0397, "GROWTH": 0.0},
        "dividends_per_share": {"T": 1.11, "GROWTH": 0.0},
        "portfolio_dividend_yield": 0.02779,
        "dividend_weight_covered": 1.0,
    }
    return _stats(**{**fields, **overrides})


def test_print_weights_and_allocation_reports_the_dividend_figures():
    out = _render(_dividend_stats(), portfolio_value=100000.0)
    assert "Dividend yield / annual income (trailing 12 months):" in out
    assert "T: yield=0.0397" in out
    assert "Portfolio dividend yield: 0.0278" in out
    assert "Annual dividend income: $2,779.00 USD (on --value $100,000.00 USD)" in out


def test_print_weights_and_allocation_names_a_non_payer_separately_from_missing_data():
    """The distinction the whole feature turns on: a confirmed non-payer
    prints a real zero and says so, while a ticker with no data prints n/a.
    """
    out = _render(_dividend_stats(), portfolio_value=100000.0)
    assert "GROWTH: yield=0.0000  $0.00 USD  (pays no dividend)" in out

    missing = _dividend_stats(
        dividend_yields={"T": 0.0397},
        dividends_per_share={"T": 1.11},
        dividend_yields_missing=("GROWTH",),
        dividend_weight_covered=0.7,
    )
    out = _render(missing, portfolio_value=100000.0)
    assert "GROWTH: yield n/a - no trailing dividend data" in out
    assert "GROWTH: yield=0.0000" not in out


def test_print_weights_and_allocation_states_the_covered_weight_when_a_yield_is_missing():
    missing = _dividend_stats(
        dividend_yields={"T": 0.0397},
        dividends_per_share={"T": 1.11},
        dividend_yields_missing=("GROWTH",),
        dividend_weight_covered=0.7,
    )
    out = _render(missing, portfolio_value=100000.0)
    assert "covers 0.7000 of the weight; GROWTH has no trailing dividend data" in out


def test_print_weights_and_allocation_omits_the_coverage_line_when_every_yield_is_known():
    out = _render(_dividend_stats(), portfolio_value=100000.0)
    assert "of the weight" not in out


def test_print_weights_and_allocation_prints_the_dividend_floor_line_with_no_floor():
    """The house convention: a line's presence never depends on how the run
    was invoked, so the floor line appears even when none was asked for.
    """
    out = _render(_dividend_stats(), portfolio_value=100000.0)
    assert (
        "Minimum dividend yield: n/a (no floor was asked for; use "
        "--min-annual-dividend or --min-dividend-yield)"
    ) in out


def test_print_weights_and_allocation_says_whether_the_dividend_floor_binds():
    binding = _dividend_stats(
        dividend_yield_floor=0.02779, dividend_floor_origin="--min-dividend-yield"
    )
    out = _render(binding, portfolio_value=100000.0)
    assert "- binding, the portfolio sits on the floor" in out

    slack = _dividend_stats(
        dividend_yield_floor=0.02, dividend_floor_origin="--min-dividend-yield"
    )
    out = _render(slack, portfolio_value=100000.0)
    assert "- not binding, the portfolio clears it by 0.0078 unaided" in out


def test_print_weights_and_allocation_names_the_flag_the_floor_came_from():
    origin = "--min-annual-dividend $3,000.00 USD / --value $100,000.00 USD"
    out = _render(
        _dividend_stats(dividend_yield_floor=0.03, dividend_floor_origin=origin),
        portfolio_value=100000.0,
    )
    assert f"Minimum dividend yield: 0.0300 ({origin})" in out


def test_print_weights_and_allocation_reports_dividends_at_the_allocated_share_counts():
    """Deliberately a different figure from the portfolio yield above it -
    whole shares plus leftover cash cannot buy continuous weights exactly.
    """
    out = _render(
        _dividend_stats(), allocation=({"T": 2500, "GROWTH": 100}, 214.87),
        portfolio_value=100000.0,
    )
    assert "Annual dividends at these share counts: $2,775.00 USD" in out
    assert "(a 0.0278 yield on --value $100,000.00 USD)" in out


def test_print_weights_and_allocation_says_so_when_an_allocated_ticker_has_no_per_share_figure():
    stats = _dividend_stats(dividends_per_share={"T": 1.11})
    out = _render(stats, allocation=({"T": 2500, "GROWTH": 100}, 0.0), portfolio_value=100000.0)
    assert (
        "Annual dividends at these share counts: n/a - no trailing "
        "dividends-per-share for GROWTH"
    ) in out


def test_print_weights_and_allocation_says_dividends_were_not_consulted_when_they_were_not():
    out = _render(_stats(weights={"T": 1.0}, expected_returns={"T": 0.08}, volatility={"T": 0.2}))
    assert (
        "Dividend yield / annual income (trailing 12 months): n/a - dividend data "
        "was not consulted for this run"
    ) in out


def test_run_edit_loop_prompt_offers_the_dividend_option(monkeypatch, stub_optimizer, capsys):
    prompts: list[str] = []

    def record(prompt=""):
        prompts.append(prompt)
        return "f"

    monkeypatch.setattr("builtins.input", record)
    _run_edit_loop(["AAPL"], "GMV", 1000.0, REBALANCE_DATE, "session.duckdb")
    assert "[d]ividend" in prompts[0]


@pytest.mark.parametrize(
    "typed,expected_yield,expects_cash_note",
    [
        ("3000", 0.03, True),
        ("3%", 0.03, False),
        ("0.03", 0.03, False),
    ],
)
def test_run_edit_loop_dividend_option_reads_each_accepted_shape(
    monkeypatch, stub_optimizer, capsys, typed, expected_yield, expects_cash_note
):
    """One prompt accepts both units the flags accept, separated by
    magnitude, with the reading echoed back so the inference can be checked.
    """
    _script(monkeypatch, "d", typed, "f")
    _run_edit_loop(["AAPL"], "GMV", 100000.0, REBALANCE_DATE, "session.duckdb")

    floor = stub_optimizer.call_args.kwargs["dividend_floor"]
    assert floor.yield_floor == pytest.approx(expected_yield)
    out = capsys.readouterr().out
    assert f"Read as a {expected_yield:.4f} minimum portfolio dividend yield" in out
    assert ("on --value $100,000.00 USD" in out) is expects_cash_note


def test_run_edit_loop_dividend_option_clears_the_floor(monkeypatch, stub_optimizer, capsys):
    _script(monkeypatch, "d", "none", "f")
    _run_edit_loop(
        ["AAPL"], "GMV", 100000.0, REBALANCE_DATE, "session.duckdb",
        dividend_floor=DividendFloor(0.03, "--min-dividend-yield"),
    )
    assert stub_optimizer.call_args.kwargs["dividend_floor"] is None
    assert "Dividend floor cleared." in capsys.readouterr().out


def test_run_edit_loop_dividend_option_reads_zero_as_clearing_the_floor(
    monkeypatch, stub_optimizer
):
    _script(monkeypatch, "d", "0", "f")
    _run_edit_loop(
        ["AAPL"], "GMV", 100000.0, REBALANCE_DATE, "session.duckdb",
        dividend_floor=DividendFloor(0.03, "--min-dividend-yield"),
    )
    assert stub_optimizer.call_args.kwargs["dividend_floor"] is None


def test_run_edit_loop_dividend_option_blank_input_skips_the_recompute(
    monkeypatch, stub_optimizer
):
    _script(monkeypatch, "d", "", "f")
    _run_edit_loop(["AAPL"], "GMV", 100000.0, REBALANCE_DATE, "session.duckdb")
    assert stub_optimizer.call_count == 0


def test_run_edit_loop_dividend_option_retyping_the_same_floor_skips_the_recompute(
    monkeypatch, stub_optimizer
):
    _script(monkeypatch, "d", "0.03", "f")
    _run_edit_loop(
        ["AAPL"], "GMV", 100000.0, REBALANCE_DATE, "session.duckdb",
        dividend_floor=DividendFloor(
            0.03, "[d]ividend, this session only", None, 100000.0, "USD"
        ),
    )
    assert stub_optimizer.call_count == 0


def test_run_edit_loop_dividend_option_unparseable_input_keeps_the_floor(
    monkeypatch, stub_optimizer, capsys
):
    _script(monkeypatch, "d", "lots", "f")
    _run_edit_loop(
        ["AAPL"], "GMV", 100000.0, REBALANCE_DATE, "session.duckdb",
        dividend_floor=DividendFloor(0.03, "--min-dividend-yield"),
    )
    assert stub_optimizer.call_count == 0
    assert "Unrecognized minimum dividend 'lots'" in capsys.readouterr().out


def test_run_edit_loop_dividend_option_refuses_a_mistyped_percentage(
    monkeypatch, stub_optimizer, capsys
):
    _script(monkeypatch, "d", "0.5", "f")
    _run_edit_loop(["AAPL"], "GMV", 100000.0, REBALANCE_DATE, "session.duckdb")
    assert stub_optimizer.call_count == 0
    assert "3% is 0.03" in capsys.readouterr().out


def test_run_edit_loop_an_unreachable_dividend_floor_reverts_instead_of_ending_the_session(
    monkeypatch, capsys
):
    """The edit loop holds live mode's only snapshot open, so an unreachable
    floor must be a rejected edit rather than a crash - which is why
    `DividendFloorError` subclasses `ValueError`.
    """
    seen: list = []

    def fake_optimizer(
        candidates, objective, value, rebalance_date, db_path, target_annual_return=0.12,
        risk_free_rate=0.02, dividend_floor=None, **_kwargs,
    ):
        seen.append(dividend_floor)
        if dividend_floor is not None and dividend_floor.yield_floor > 0.1:
            raise DividendFloorError("the highest-yielding candidate is T at 0.0397")
        return _dividend_stats(), ({}, 0.0)

    monkeypatch.setattr("src.flow.cli.compute_weights_and_allocation", fake_optimizer)
    monkeypatch.setattr("src.flow.cli.print_weights_and_allocation", lambda *a, **k: None)
    # A floor the VALIDATOR accepts (it is under MAX_DIVIDEND_YIELD) but this
    # pool cannot reach, then an unrelated edit whose recompute shows what
    # actually survived the revert. A floor the validator would refuse never
    # reaches the optimizer at all, which is a different path - see
    # `test_run_edit_loop_dividend_option_refuses_a_mistyped_percentage`.
    _script(monkeypatch, "d", "0.2", "o", "MSR", "f")

    _run_edit_loop(
        ["AAPL"], "GMV", 100000.0, REBALANCE_DATE, "session.duckdb",
        dividend_floor=DividendFloor(0.02, "--min-dividend-yield"),
    )

    out = capsys.readouterr().out
    assert "Cannot optimize that edit: the highest-yielding candidate is T at 0.0397" in out
    assert "Keeping the previous candidates, objective, target return, and dividend floor." in out
    # The rejected 0.2 was replaced by the original 0.02, not left in place.
    assert [f.yield_floor for f in seen] == [0.2, 0.02]


def test_run_edit_loop_carries_the_dividend_floor_into_an_unrelated_recompute(
    monkeypatch, stub_optimizer
):
    _script(monkeypatch, "o", "MSR", "f")
    _run_edit_loop(
        ["AAPL"], "GMV", 100000.0, REBALANCE_DATE, "session.duckdb",
        dividend_floor=DividendFloor(0.03, "--min-dividend-yield"),
    )
    assert stub_optimizer.call_args.kwargs["dividend_floor"].yield_floor == 0.03


def test_settle_dividend_floor_divides_a_cash_amount_by_value():
    floor = _settle_dividend_floor(3000.0, None, 100000.0, "USD")
    assert floor.yield_floor == pytest.approx(0.03)
    assert floor.cash_floor == 3000.0
    assert floor.origin == (
        "--min-annual-dividend $3,000.00 USD / --value $100,000.00 USD"
    )


def test_settle_dividend_floor_passes_a_yield_through_naming_its_flag():
    floor = _settle_dividend_floor(None, 0.03, 100000.0, "USD")
    assert floor.yield_floor == 0.03
    assert floor.origin == "--min-dividend-yield"
    assert floor.cash_floor is None


def test_settle_dividend_floor_is_none_when_neither_flag_was_given():
    assert _settle_dividend_floor(None, None, 100000.0, "USD") is None


def test_settle_dividend_floor_keeps_an_explicit_zero_as_a_floor():
    """A scripted `--min-dividend-yield 0` is a deliberate no-op somebody
    typed, and a different thing from no floor at all - so the report prints
    it as a floor.
    """
    floor = _settle_dividend_floor(None, 0.0, 100000.0, "USD")
    assert floor is not None
    assert floor.yield_floor == 0.0


def test_settle_dividend_floor_refuses_both_flags_rather_than_reconciling_them():
    with pytest.raises(ValueError, match="two spellings of one"):
        _settle_dividend_floor(3000.0, 0.03, 100000.0, "USD")


# ==========================================================================
# Split restatement and the stale share-count warning
# ==========================================================================


def _split_context(
    ex_date=date(2025, 12, 29), ratio=4.0, payments=((date(2025, 9, 29), 5.5), (date(2026, 3, 30), 5.5))
):
    from src.dataset.dividends import SplitContext

    return SplitContext(splits=[(ex_date, ratio)], payments=list(payments))


def test_format_split_restatement_reconciles_the_announced_amount():
    """The line that answers "why does the report say 5.5 when the company
    announced 22?" — using only stored data and the split ratio.
    """
    line = format_split_restatement(
        {"9984.T": _split_context()}, {"9984.T": 11.0}, "JPY"
    )
    assert "Per-share amounts are on each ticker's CURRENT share basis." in line
    assert "9984.T split 4:1 on 2025-12-29" in line
    assert "2025-09-29 payment of ¥22.00 JPY as announced" in line
    assert "counts as ¥5.50 JPY per current share" in line


def test_format_split_restatement_is_none_when_nothing_split():
    """So the overwhelming majority of reports are byte-identical to before
    this feature existed.
    """
    assert format_split_restatement({}, {"KO": 1.94}, "USD") is None
    assert format_split_restatement(None, None, "USD") is None


def test_format_split_restatement_names_only_a_payment_the_split_restated():
    """A window whose only payment came AFTER the split has nothing to
    reconcile, so the sentence stops at the split itself rather than
    claiming a restatement that did not happen.
    """
    line = format_split_restatement(
        {"9984.T": _split_context(payments=((date(2026, 3, 30), 5.5),))},
        {"9984.T": 5.5},
        "JPY",
    )
    assert "9984.T split 4:1 on 2025-12-29" in line
    assert "as announced" not in line


def test_format_split_restatement_covers_two_splits_in_one_window():
    from src.dataset.dividends import SplitContext

    line = format_split_restatement(
        {
            "X": SplitContext(
                splits=[(date(2026, 1, 5), 2.0), (date(2026, 6, 5), 3.0)],
                payments=[(date(2025, 12, 1), 6.0)],
            )
        },
        {"X": 6.0},
        "USD",
    )
    assert "X split 2:1 on 2026-01-05 and 3:1 on 2026-06-05" in line
    assert "$36.00 USD as announced" in line


def test_print_weights_and_allocation_explains_a_split_in_the_window():
    stats = _dividend_stats(dividend_splits={"T": _split_context()})
    out = _render(stats, portfolio_value=100000.0)
    assert "CURRENT share basis" in out


def test_print_weights_and_allocation_omits_the_split_line_when_nothing_split():
    out = _render(_dividend_stats(), portfolio_value=100000.0)
    assert "CURRENT share basis" not in out


def test_format_stale_share_counts_warns_and_prints_a_runnable_command(tmp_path):
    """The suggested command has to work verbatim: a share count with
    thousands separators would not parse, so a suggestion that has to be
    edited first is no suggestion at all.
    """
    from src.dataset.dividends import write_dividends_tables, coverage_frame
    from src.flow.user_portfolio import save_portfolio

    portfolio = str(tmp_path / "p.json")
    save_portfolio({"9984.T": 1000.0}, path=portfolio, currency="JPY")
    # Backdate the save to before the split.
    raw = json.loads(Path(portfolio).read_text())
    raw["portfolios"]["JPY"]["updated_at"] = "2025-11-02T00:00:00+00:00"
    Path(portfolio).write_text(json.dumps(raw))

    db = str(tmp_path / "cache.duckdb")
    write_dividends_tables(
        pd.DataFrame(columns=["ex_date", "ticker", "amount"]),
        coverage_frame(["9984.T"], set(), "2021-01-01", "2026-09-08"),
        db,
        splits_df=pd.DataFrame(
            {"ex_date": pd.to_datetime(["2025-12-29"]), "ticker": ["9984.T"], "ratio": [4.0]}
        ),
    )

    warning = format_stale_share_counts(portfolio, "JPY", {"9984.T": 1000.0}, db)
    assert "9984.T split 4:1 on 2025-12-29" in warning
    assert "last updated (2025-11-02)" in warning
    assert "understate every figure below by 4x" in warning
    assert "uv run portfolio-holdings set 9984.T 4000" in warning
    assert "4,000 now" in warning  # readable in prose
    assert "set 9984.T 4,000" not in warning  # but never in the command


def test_format_stale_share_counts_is_silent_when_the_count_postdates_the_split(tmp_path):
    from src.dataset.dividends import write_dividends_tables, coverage_frame
    from src.flow.user_portfolio import save_portfolio

    portfolio = str(tmp_path / "p.json")
    save_portfolio({"9984.T": 1000.0}, path=portfolio, currency="JPY")

    db = str(tmp_path / "cache.duckdb")
    write_dividends_tables(
        pd.DataFrame(columns=["ex_date", "ticker", "amount"]),
        coverage_frame(["9984.T"], set(), "2021-01-01", "2026-09-08"),
        db,
        splits_df=pd.DataFrame(
            {"ex_date": pd.to_datetime(["2025-12-29"]), "ticker": ["9984.T"], "ratio": [4.0]}
        ),
    )
    assert format_stale_share_counts(portfolio, "JPY", {"9984.T": 1000.0}, db) is None


def test_format_stale_share_counts_is_silent_without_a_splits_table(tmp_path):
    """A database predating the splits table must change no existing
    output.
    """
    from src.flow.user_portfolio import save_portfolio

    portfolio = str(tmp_path / "p.json")
    save_portfolio({"9984.T": 1000.0}, path=portfolio, currency="JPY")
    assert format_stale_share_counts(
        portfolio, "JPY", {"9984.T": 1000.0}, str(tmp_path / "absent.duckdb")
    ) is None


def test_format_stale_share_counts_is_silent_for_an_empty_portfolio(tmp_path):
    assert format_stale_share_counts(str(tmp_path / "p.json"), "JPY", {}, "x.duckdb") is None


# ==========================================================================
# --no-dividend-fetch
# ==========================================================================


@pytest.mark.parametrize(
    "floor_flag", [["--min-dividend-yield", "0.02"], ["--min-annual-dividend", "3000"]]
)
def test_main_refuses_no_dividend_fetch_alongside_a_dividend_floor(
    monkeypatch, capsys, floor_flag
):
    """A contradiction: the floor is enforced against the very data the flag
    declines to fetch. Refused at the door, before a live snapshot is paid
    for - the optimizer's own complaint would name every ticker in the pool,
    which is a poor way to learn you typed two incompatible flags.
    """
    session_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.open_pipeline_session", session_spy)
    monkeypatch.setattr(
        "sys.argv",
        ["portfolio", "--date", "today", "--objective", "GMV", "--value", "100000",
         "--no-dividend-fetch", *floor_flag],
    )

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "--no-dividend-fetch cannot be combined with" in err
    session_spy.assert_not_called()


def test_main_threads_the_dividend_fetch_choice_into_the_session_and_the_pipeline(monkeypatch):
    seen: dict = {}

    @contextlib.contextmanager
    def session(rebalance_date, selection, db_path, allow_dividend_fetch=True):
        seen["allow_fetch"] = allow_dividend_fetch
        yield "session.duckdb", "live"

    def pipeline(*_a, **kwargs):
        seen["consult"] = kwargs.get("consult_dividends")
        return {
            "mode": "live", "rebalance_date": REBALANCE_DATE, "objective": "GMV",
            "selection": "llm_s_only", "rule": None,
            "scan_detail": {"branch": "b", "buy_s_size": 1, "buy_f_size": 0,
                            "intersection_size": 0, "union_size": 1, "candidates": ["AAA"]},
            "weights": {"AAA": 1.0}, "allocation": ({}, 0.0), "stats": _stats(),
            "currency": "USD", "benchmark": None,
        }

    monkeypatch.setattr("src.flow.cli.open_pipeline_session", session)
    monkeypatch.setattr("src.flow.cli.run_pipeline_against", pipeline)
    monkeypatch.setattr("src.flow.cli.print_pipeline_result", lambda *a, **k: None)
    monkeypatch.setattr("src.flow.cli._run_edit_loop", lambda *a, **k: None)
    monkeypatch.setattr(
        "sys.argv",
        ["portfolio", "--date", "today", "--objective", "GMV", "--value", "100000",
         "--no-dividend-fetch", "--no-holdings", "--benchmark", "none"],
    )

    main()

    assert seen["allow_fetch"] is False
    assert seen["consult"] is False


def test_print_weights_and_allocation_says_dividends_were_not_consulted_when_skipped():
    """`--no-dividend-fetch` must land on the "not consulted" wording, not on
    the "consulted, nothing found" wording that names each ticker's reason -
    it never looked, so implying a failed lookup would be false.
    """
    out = _render(_stats(weights={"AAA": 1.0}, expected_returns={"AAA": 0.1},
                         volatility={"AAA": 0.2}))
    assert "n/a - dividend data was not consulted for this run" in out
    assert "no trailing dividend data" not in out


def test_run_edit_loop_refuses_a_dividend_floor_when_dividends_were_not_consulted(
    monkeypatch, stub_optimizer, capsys
):
    """The same contradiction reached from inside the loop rather than from
    the command line.
    """
    _script(monkeypatch, "d", "f")

    _run_edit_loop(
        ["AAPL"], "GMV", 100000.0, REBALANCE_DATE, "session.duckdb", consult_dividends=False
    )

    out = capsys.readouterr().out
    assert "A dividend floor needs dividend data" in out
    assert stub_optimizer.call_count == 0
