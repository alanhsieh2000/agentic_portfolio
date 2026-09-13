"""`src/agentic_portfolio/config/preflight.py`: refuse a run that cannot
succeed, before it costs anything.

These tests monkeypatch both `Settings` and `os.environ`, and that is not
belt-and-braces - it is the only way to test the refusal path in this
repository at all. CrewAI calls `load_dotenv()` at import time, and
`load_dotenv` searches upward from CrewAI's own installed file, which in a
development checkout is `<repo>/.venv/lib/.../crewai/`. So importing anything
that reaches CrewAI injects `<repo>/.env` - including the developer's real
`ANTHROPIC_API_KEY` - into `os.environ` whatever the working directory is.
A test that merely unset the variable would therefore be testing nothing.
"""

from __future__ import annotations

import pytest

from agentic_portfolio.config import preflight
from agentic_portfolio.config.settings import settings

PROVIDER_VARIABLES = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")


def _no_keys_anywhere(monkeypatch) -> None:
    """Neither `Settings` nor the environment holds a provider key."""
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    monkeypatch.setattr(settings, "openai_api_key", None)
    for variable in PROVIDER_VARIABLES:
        monkeypatch.delenv(variable, raising=False)


@pytest.mark.parametrize("selection", ["llm_s_only", "llm_f_only", "llm_s_and_f"])
def test_a_selection_needing_an_llm_is_refused_without_a_key(monkeypatch, capsys, selection):
    _no_keys_anywhere(monkeypatch)
    with pytest.raises(SystemExit):
        preflight.require_api_keys(selection)
    message = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in message, "the message must name the variable to set"
    assert settings.llm_s_model in message or settings.llm_f_model in message, (
        "the message must name the model, since changing it is one of the ways out"
    )
    assert "user_provided" in message, "the message must offer the selection that needs no key"


def test_user_provided_is_never_refused(monkeypatch):
    """The one selection that takes its candidates from the user and calls no
    LLM must keep working with no credentials at all - that is the whole reason
    someone with no API key can still use this tool."""
    _no_keys_anywhere(monkeypatch)
    preflight.require_api_keys("user_provided")


@pytest.mark.parametrize("selection", ["llm_s_only", "llm_f_only", "llm_s_and_f", "user_provided"])
def test_nothing_is_refused_when_the_key_is_present(monkeypatch, selection):
    _no_keys_anywhere(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    preflight.require_api_keys(selection)


def test_a_key_in_settings_alone_is_accepted(monkeypatch):
    """`Settings` is consulted BEFORE the environment, because pydantic-settings
    loads `.env` without exporting it - so a machine whose key lives only in
    `.env` must not be refused."""
    _no_keys_anywhere(monkeypatch)
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-from-dotenv")
    preflight.require_api_keys("llm_s_and_f")


def test_a_key_in_settings_is_exported_so_the_sdk_can_see_it(monkeypatch):
    """The gap this closes: the SDKs read `os.environ` only, and in the Docker
    image CrewAI's own `load_dotenv()` cannot reach the mounted workspace,
    because it searches upward from `/opt/agentic-portfolio/venv` rather than
    from the working directory."""
    _no_keys_anywhere(monkeypatch)
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-from-dotenv")
    preflight.export_api_keys()
    import os

    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-from-dotenv"


def test_an_explicit_environment_key_is_never_overridden(monkeypatch):
    """`-e ANTHROPIC_API_KEY=...` on the command line must beat a stale `.env`."""
    _no_keys_anywhere(monkeypatch)
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-from-dotenv")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-explicit")
    preflight.export_api_keys()
    import os

    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-explicit"


def test_the_model_that_is_checked_follows_the_setting(monkeypatch, capsys):
    """The preflight must read the model from `Settings` rather than re-derive
    it, or it would eventually vouch for a key the run does not use. Point
    LLM-S at an OpenAI model and the OpenAI variable becomes the one named."""
    _no_keys_anywhere(monkeypatch)
    monkeypatch.setattr(settings, "llm_s_model", "openai/gpt-5-nano")
    with pytest.raises(SystemExit):
        preflight.require_api_keys("llm_s_only")
    assert "OPENAI_API_KEY" in capsys.readouterr().err


def test_sec_user_agent_is_required_and_names_the_variable(monkeypatch, capsys):
    monkeypatch.setattr(settings, "sec_ua", "")
    monkeypatch.delenv("SEC_UA", raising=False)
    with pytest.raises(SystemExit):
        preflight.require_sec_user_agent()
    message = capsys.readouterr().err
    assert "SEC_UA" in message
    assert "403" in message, "the message should say what the SEC actually does without it"


def test_a_blank_sec_user_agent_counts_as_missing(monkeypatch):
    """Whitespace is not a descriptive User-Agent, and the SEC rejects it the
    same way it rejects an absent one."""
    monkeypatch.setattr(settings, "sec_ua", "   ")
    monkeypatch.delenv("SEC_UA", raising=False)
    with pytest.raises(SystemExit):
        preflight.require_sec_user_agent()


def test_sec_user_agent_from_settings_alone_is_accepted(monkeypatch):
    monkeypatch.setattr(settings, "sec_ua", "Test User test@example.com")
    monkeypatch.delenv("SEC_UA", raising=False)
    preflight.require_sec_user_agent()


def test_the_refusal_exits_two_not_one(monkeypatch, capsys):
    """2 is argparse's exit code for a usage error, which is what a missing
    credential is. Keeping it distinct from 1 lets a script tell a
    misconfiguration apart from a data failure.

    Worth asserting rather than assuming: `raise SystemExit(message)` sets
    `.code` to the string and exits 1, so the message and the status have to be
    set separately. This test fails if anyone simplifies that back."""
    _no_keys_anywhere(monkeypatch)
    with pytest.raises(SystemExit) as refusal:
        preflight.require_api_keys("llm_s_only")
    assert refusal.value.code == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err, "the sentence must still reach stderr"


def test_the_sec_refusal_also_exits_two(monkeypatch, capsys):
    monkeypatch.setattr(settings, "sec_ua", "")
    monkeypatch.delenv("SEC_UA", raising=False)
    with pytest.raises(SystemExit) as refusal:
        preflight.require_sec_user_agent()
    assert refusal.value.code == 2
    assert "SEC_UA" in capsys.readouterr().err
