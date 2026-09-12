"""Exception types shared across every layer of this project.

Deliberately at the top of `src/agentic_portfolio/` with NO imports of its own. The types
here are raised in the dataset layer (`src/agentic_portfolio/dataset/ticker_currency.py`), in
the optimizer (`src/agentic_portfolio/optimizer/portfolio.py`,
`src/agentic_portfolio/optimizer/dividends.py`) and caught in the flow layer
(`src/agentic_portfolio/flow/cli.py`), so anywhere else would create an import cycle for at
least one of them.
"""

from __future__ import annotations


class UnsatisfiableRequestError(ValueError):
    """The optimizer cannot satisfy what was asked, and no code change would
    help: the numbers on the command line are mutually impossible for this
    pool of candidates.

    Asking MV for a 12% return while requiring a 3% dividend yield from a
    pool whose best complying portfolio reaches 10.97% is the canonical
    case. So is asking for a target return no combination of these tickers
    can produce, a risk-free rate above every candidate's expected return
    under MSR, a dividend floor above the highest-yielding candidate's own
    yield, or a portfolio that mixes two currencies.

    Every one of these is a statement about the REQUEST rather than about
    the code, which is why they are gathered under one type. A caller that
    catches this can present the message and let the person change a number;
    a caller that catches `ValueError` instead would also swallow genuine
    bugs, and `src/agentic_portfolio/flow/cli.py`'s `main` is exactly where that distinction
    has to hold.

    Subclasses `ValueError` so that every existing handler keeps working -
    notably `src/agentic_portfolio/flow/cli.py`'s interactive edit loop, which reverts a
    rejected edit on `except ValueError` and predates this type. Each member
    lives where it is raised and documented rather than being collected
    here, so the reasoning stays next to the code that knows it.
    """
