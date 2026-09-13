"""The eight CrewAI prompt files and the PDF stylesheet must be reachable as
package data.

Why this test exists, given that the files are obviously sitting in the source
tree. Every word of the LLM-S, LLM-F and summary prompts lives in those YAML
files, and CrewAI reads them itself - resolving the relative
`agents_config = "config/agents.yaml"` against each crew module's own file
location. That works in a source checkout unconditionally. In an installed copy
it works only if the wheel actually contains them, and hatchling includes
non-Python files inside a `packages` directory by convention rather than by an
explicit declaration in `pyproject.toml`.

So the failure this guards against is a packaging change - a `.gitignore` edit, a
switch to a different build backend, an `exclude` rule added for tidiness - that
drops the YAML from the wheel. CrewAI would not complain: its `_load_config`
logs a warning and proceeds with an empty dictionary, so the first symptom used
to be a bare `KeyError` naming an agent. `agentic_portfolio/agents/crew_config.py`
now turns that into a sentence naming the file, and the last test here pins that.

`agentic_portfolio/flow/report_pdf.css` rides the same convention for the same
reason, and is guarded here for the same failure: a wheel without it renders
every PDF at WeasyPrint's defaults - US Letter, no margins worth the name, and a
proportional face that destroys the column alignment the whole file exists to
preserve - rather than failing outright.

These tests use `importlib.resources`, not `Path(__file__).parent`, on purpose:
the former asks the import system where the data actually is, which is the same
question CrewAI's loader is effectively asking, and it keeps answering correctly
for an installed package rather than only for a checkout.
"""

from __future__ import annotations

from importlib.resources import files

import pytest

from agentic_portfolio.agents.crew_config import CrewConfigMissing, require

CREW_PACKAGES = ("llm_s_crew", "llm_f_crew", "summary_crew", "translate_crew")
CONFIG_FILES = ("agents.yaml", "tasks.yaml")


@pytest.mark.parametrize("crew", CREW_PACKAGES)
@pytest.mark.parametrize("filename", CONFIG_FILES)
def test_each_crew_prompt_file_is_reachable_as_package_data(crew, filename):
    resource = files(f"agentic_portfolio.agents.{crew}").joinpath("config", filename)
    assert resource.is_file(), (
        f"{crew}/config/{filename} is not reachable through the import system. If this is an "
        "installed copy, the wheel was built without its YAML files and every agent would be "
        "built from empty configuration"
    )


@pytest.mark.parametrize("crew", CREW_PACKAGES)
@pytest.mark.parametrize("filename", CONFIG_FILES)
def test_each_crew_prompt_file_has_content(crew, filename):
    """A present but empty file would pass the existence check above and still
    leave the agents unconfigured."""
    text = files(f"agentic_portfolio.agents.{crew}").joinpath("config", filename).read_text()
    assert text.strip(), f"{crew}/config/{filename} is empty"


def test_an_empty_config_names_the_file_rather_than_raising_keyerror():
    """The whole point of `require`: CrewAI hands over `{}` for a missing file,
    and the old behavior turned that into `KeyError: 'strategy_agent'`, which
    names neither a file nor a cause."""
    with pytest.raises(CrewConfigMissing) as failure:
        require({}, "strategy_agent", "agentic_portfolio/agents/llm_s_crew/config/agents.yaml")
    message = str(failure.value)
    assert "agents.yaml" in message, "the error must name the file that should have been there"
    assert "wheel" in message, "the error should point at the likely cause for an installed copy"


def test_a_config_missing_one_key_says_what_it_does_define():
    with pytest.raises(CrewConfigMissing) as failure:
        require({"sentiment_agent": {}}, "strategy_agent", "config/agents.yaml")
    message = str(failure.value)
    assert "strategy_agent" in message
    assert "sentiment_agent" in message, "naming the available keys is what makes this diagnosable"


def test_a_present_key_is_returned_unchanged():
    config = {"strategy_agent": {"role": "analyst"}}
    assert require(config, "strategy_agent", "config/agents.yaml") == {"role": "analyst"}


def test_the_pdf_stylesheet_is_reachable_as_package_data():
    resource = files("agentic_portfolio.flow").joinpath("report_pdf.css")
    assert resource.is_file(), (
        "agentic_portfolio/flow/report_pdf.css is not reachable through the import system. "
        "If this is an installed copy, the wheel was built without it and every briefing "
        "would be rendered at WeasyPrint's default page size with proportional type, which "
        "breaks the column alignment of every table"
    )


def test_the_pdf_stylesheet_sets_a_page_size_and_preserves_whitespace():
    """A present but truncated stylesheet would pass the check above and still
    lay every table out wrongly, so the two declarations that actually carry the
    tables are pinned by name."""
    text = files("agentic_portfolio.flow").joinpath("report_pdf.css").read_text()
    assert "@page" in text
    assert "size: A4" in text
    # `pre-wrap` rather than `pre`: WeasyPrint does not paginate horizontally,
    # so an over-wide line under plain `pre` runs off the paper silently.
    assert "pre-wrap" in text
