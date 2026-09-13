"""Refuse a run that cannot possibly succeed, before it costs anything.

Why this exists. Several entry points spend minutes fetching data before they
reach the thing they need a credential for. A live-mode screened run fetches
Wikipedia membership, sixty-five months of prices for roughly five hundred
tickers, factors, momentum, returns and dividends - and only then calls LLM-S.
With no `ANTHROPIC_API_KEY` the whole of that work is thrown away and the user
sees a provider exception from inside CrewAI's executor, which names neither the
variable that is missing nor a way forward. `portfolio-build-fundamentals` has
the same shape around `SEC_UA`: the SEC rejects a request without a descriptive
User-Agent, but the Wikipedia and price fetches happen first.

So the checks here run immediately after argument parsing, cost nothing, and
either say nothing or exit with one sentence naming the variable, what cannot
happen without it, and the concrete ways out.

This module lives in `config/` rather than `agents/` deliberately: it must be
importable without pulling in `crewai`, which `agents/report_summary.py` cannot
be. `api_key_problem` was moved here from that module for the same reason and is
re-exported there, so its existing callers and monkeypatch targets still resolve.

Where a key may live, which is worth stating because two different readers are
involved. This project's own `Settings` reads `.env` from
`AGENTIC_PORTFOLIO_HOME` (default: the working directory) and does NOT export it
to the process environment. CrewAI separately calls `load_dotenv()`, which reads
`.env` from the **current working directory**. Those agree in the default case
and in the Docker image, which is why a key in `.env` works at all. They diverge
only if `AGENTIC_PORTFOLIO_HOME` is pointed elsewhere, so the messages below
name both places rather than just `.env`.

Each refusal prints its sentence to stderr and raises `SystemExit(2)`. The code
matters: 2 is argparse's exit status for a usage error, which is what a missing
credential is, and keeping it distinct from 1 lets anything scripting these
commands tell a misconfiguration apart from a data failure. Note that passing
the message to `SystemExit` directly would NOT do this - `SystemExit("text")`
sets `.code` to the string and exits 1 - so the message and the status are set
separately.
"""

from __future__ import annotations

import os
import sys

from agentic_portfolio.config.settings import settings

#: Model-name prefix to the environment variable that authenticates it, and the
#: `Settings` field that may hold it instead. Keyed by prefix because model names
#: in this project are litellm-style (`anthropic/claude-sonnet-4-5`), so the
#: provider is the part before the slash.
_PROVIDERS: dict[str, tuple[str, str]] = {
    "openai/": ("OPENAI_API_KEY", "openai_api_key"),
    "anthropic/": ("ANTHROPIC_API_KEY", "anthropic_api_key"),
}

#: Which agent each selection needs, and therefore which model's provider key
#: must be present. `user_provided` is absent on purpose: that selection takes
#: its candidates from the user and never calls an LLM, so it must keep working
#: with no key at all.
_SELECTION_AGENTS: dict[str, tuple[str, ...]] = {
    "llm_s_only": ("llm_s",),
    "llm_f_only": ("llm_f",),
    "llm_s_and_f": ("llm_s", "llm_f"),
    "user_provided": (),
}

_AGENT_LABELS: dict[str, str] = {
    "llm_s": "the LLM-S screening rule",
    "llm_f": "the LLM-F sentiment scores",
}


def _refuse(message: str) -> None:
    """Print `message` and exit 2.

    Written this way because `raise SystemExit(message)` sets `.code` to the
    string and exits 1, which would lose the usage-error status. Declared
    `-> None` rather than `-> NoReturn` only to keep the call sites readable;
    it never returns.
    """
    print(message, file=sys.stderr)
    raise SystemExit(2)


#: What `api_key_problem` says a missing key costs, and how to proceed without
#: it, when the caller does not say. These are the summary's prose pass, which
#: was this check's only caller when it was written.
DEFAULT_PURPOSE = "the prose for this summary cannot be written"
DEFAULT_REMEDY = "re-run with --no-llm to get the figures without the prose"


def api_key_problem(
    model: str,
    purpose: str = DEFAULT_PURPOSE,
    remedy: str = DEFAULT_REMEDY,
) -> str | None:
    """The reason this model cannot be reached, or `None` if it can.

    Checked before a crew is built, so an unreachable model costs nothing and
    fails with a sentence rather than from inside CrewAI's executor.

    The provider's key is looked for in `Settings` FIRST and the process
    environment second, and that order is not cosmetic: `pydantic_settings`
    loads `.env` without exporting it to `os.environ`, so a check against the
    environment alone would refuse to run on a machine whose key lives only in
    `.env` - which is how this repository is configured.

    `purpose` and `remedy` are parameters rather than fixed text because there
    is now more than one caller and they do not have the same way out. The
    summary's prose pass can fall back to `--no-llm`; the translation in
    `src/agentic_portfolio/agents/report_translation.py` cannot, since
    `--no-llm` is refused alongside `--language` and dropping `--language` is
    the actual remedy there. Telling a user to pass a flag that would be
    rejected is worse than saying nothing. The defaults preserve this
    function's original wording exactly, so every existing call site and test
    is unaffected.
    """
    for prefix, (variable, field) in _PROVIDERS.items():
        if model.startswith(prefix) and not (getattr(settings, field, None) or os.environ.get(variable)):
            return (
                f"{variable} is not set, so {purpose} with {model}. Set it in .env or the "
                f"environment, choose another model with LLM_QUICK or --model, or {remedy}"
            )
    return None


def _missing_key(model: str) -> str | None:
    """The environment variable this model needs and does not have, if any."""
    for prefix, (variable, field) in _PROVIDERS.items():
        if model.startswith(prefix) and not (getattr(settings, field, None) or os.environ.get(variable)):
            return variable
    return None


def export_api_keys() -> None:
    """Copy any provider key that `Settings` has and the environment lacks into
    `os.environ`, so the LLM SDKs can actually see it.

    This exists because two different readers are involved and they do not look
    in the same place. `Settings` reads `.env` from `AGENTIC_PORTFOLIO_HOME`
    (default: the working directory) and deliberately does NOT export it.
    litellm and the Anthropic/OpenAI SDKs underneath CrewAI read `os.environ`
    only. CrewAI does call `load_dotenv()` itself, which papers over the gap in
    a development checkout - but only by accident of layout: `load_dotenv`
    searches upward from CrewAI's own installed file, and in a checkout that is
    `<repo>/.venv/lib/.../crewai/`, so it finds `<repo>/.env` whatever the
    working directory is.

    That accident does not survive packaging. In the Docker image the virtual
    environment lives at `/opt/agentic-portfolio/venv`, so searching upward from
    it reaches `/opt/agentic-portfolio/` and `/` - never the user's mounted
    `/work`. Without this function a `.env` in the mounted workspace would
    satisfy the preflight below and then fail inside the provider call, which is
    the worst of both: a check that passes and a run that dies minutes later.

    Never overrides a variable already set, so an explicit `-e ANTHROPIC_API_KEY`
    or a real shell export always wins over a file.
    """
    for _prefix, (variable, field) in _PROVIDERS.items():
        value = getattr(settings, field, None)
        if value and not os.environ.get(variable):
            os.environ[variable] = value


def require_api_keys(selection: str) -> None:
    """Exit unless every agent `selection` uses has a reachable model.

    Says nothing for `user_provided`, which calls no LLM. Reads the model names
    from `Settings` rather than re-deriving them, so this cannot vouch for a key
    the run will not actually use.
    """
    export_api_keys()
    models = {"llm_s": settings.llm_s_model, "llm_f": settings.llm_f_model}
    for agent in _SELECTION_AGENTS.get(selection, ()):
        model = models[agent]
        variable = _missing_key(model)
        if variable is None:
            continue
        _refuse(
            f"{variable} is not set, so {_AGENT_LABELS[agent]} for --selection {selection} "
            f"cannot be generated with {model}. Set it in the environment (or in a .env file "
            "in the directory you run from), point LLM_S_MODEL or LLM_F_MODEL at a model whose "
            "provider you do have a key for, or re-run with --selection user_provided, which "
            "needs no key"
        )


def require_sec_user_agent() -> None:
    """Exit unless `SEC_UA` is set to something non-blank.

    The SEC returns HTTP 403 for a request without a descriptive User-Agent.
    `src/agentic_portfolio/dataset/sec_edgar.py` already raises a clear error,
    but only once a fetch is attempted - which is after the Wikipedia and price
    passes. This is the same fact, established before any of that runs.
    """
    if (settings.sec_ua or os.environ.get("SEC_UA", "")).strip():
        return
    _refuse(
        "SEC_UA is not set, so the book-equity figures cannot be fetched - the SEC requires a "
        "descriptive User-Agent and returns HTTP 403 without one. Set it to something like "
        '"Your Name your.email@example.com" in the environment, or in a .env file in the '
        "directory you run from"
    )
