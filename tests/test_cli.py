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

from contextlib import contextmanager
from datetime import date
from unittest.mock import MagicMock

import pytest

from src.flow.cli import (
    _run_edit_loop,
    _run_user_provided_confirm_loop,
    main,
    print_weights_and_allocation,
)
from src.config.settings import settings
from src.optimizer.portfolio import DEFAULT_TARGET_ANNUAL_RETURN, PortfolioStats

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
    assert "Leftover cash: $12.34" in out


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


def _stub_main_pipeline(monkeypatch) -> tuple[MagicMock, MagicMock]:
    """Stub everything `main` calls after argument parsing, returning the
    `run_pipeline_against` and `_run_edit_loop` spies.
    """

    @contextmanager
    def fake_session(rebalance_date, selection, db_path):
        yield "session.duckdb", "backtest"

    pipeline_spy = MagicMock(return_value={"scan_detail": {"candidates": ["AAPL"]}})
    edit_spy = MagicMock()
    monkeypatch.setattr("src.flow.cli.open_pipeline_session", fake_session)
    monkeypatch.setattr("src.flow.cli.run_pipeline_against", pipeline_spy)
    monkeypatch.setattr("src.flow.cli.print_pipeline_result", lambda result: None)
    monkeypatch.setattr("src.flow.cli._run_edit_loop", edit_spy)
    return pipeline_spy, edit_spy


def test_main_threads_the_target_return_argument_into_the_pipeline_and_the_edit_loop(monkeypatch):
    pipeline_spy, edit_spy = _stub_main_pipeline(monkeypatch)
    monkeypatch.setattr(
        "sys.argv",
        ["portfolio", "--date", "2024-03-01", "--objective", "MV", "--value", "1000", "--target-return", "0.09"],
    )

    main()

    assert pipeline_spy.call_args.kwargs["target_annual_return"] == 0.09
    assert edit_spy.call_args.kwargs["target_annual_return"] == 0.09


def test_main_target_return_defaults_when_the_argument_is_omitted(monkeypatch):
    pipeline_spy, _edit_spy = _stub_main_pipeline(monkeypatch)
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
    pipeline_spy, edit_spy = _stub_main_pipeline(monkeypatch)
    monkeypatch.setattr(
        "sys.argv",
        ["portfolio", "--date", "2024-03-01", "--objective", "MSR", "--value", "1000",
         "--risk-free-rate", "0.015"],
    )

    main()

    assert pipeline_spy.call_args.kwargs["risk_free_rate"] == 0.015
    assert edit_spy.call_args.kwargs["risk_free_rate"] == 0.015


def test_main_risk_free_rate_defaults_to_the_configured_setting(monkeypatch):
    pipeline_spy, _edit_spy = _stub_main_pipeline(monkeypatch)
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
