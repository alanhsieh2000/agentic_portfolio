"""One helper, shared by all three crews: turn a missing prompt file into a
sentence that names it.

Why this is needed. Every word of the LLM-S, LLM-F and summary prompts lives in
`agents/<name>_crew/config/agents.yaml` and `.../tasks.yaml` - six files, none
of them Python. CrewAI loads them itself, resolving the relative
`agents_config = "config/agents.yaml"` against the crew module's own file
location rather than the working directory, which is what makes them work from
an installed wheel at all.

The failure mode this closes is CrewAI's, not ours. Its `_load_config` catches a
missing file, logs a warning, and carries on with an empty dictionary. The next
thing that happens is `self.agents_config["strategy_agent"]`, which raises a bare
`KeyError: 'strategy_agent'` - a message that names neither a file nor a reason,
and that a reader will spend a long time mistaking for a typo in the agent name.

That matters more after packaging than before. In a development checkout the YAML
sits beside the source and is always present; in a wheel it is present only if
the build included it, and hatchling includes non-Python files by convention
rather than by an explicit declaration. So the one plausible cause of a
`KeyError` here is a packaging mistake, and this makes the error say so.
"""

from __future__ import annotations

from typing import Any


class CrewConfigMissing(RuntimeError):
    """A crew's prompt configuration could not be read.

    Raised rather than returning a default, because a crew built from empty
    configuration would produce an agent with no role, goal or backstory and
    call the model anyway - spending money to get a confidently wrong answer
    from a prompt nobody wrote.
    """


def require(config: Any, key: str, path: str) -> Any:
    """`config[key]`, or a `CrewConfigMissing` naming `path`.

    `config` is whatever CrewAI put on the class attribute: normally the parsed
    YAML mapping, but an empty dictionary when the file was not found.
    """
    if not config:
        raise CrewConfigMissing(
            f"{path} is empty or was not found, so this crew's prompts cannot be loaded. "
            "It ships inside the installed package beside its crew module - if this is an "
            "installed copy, the wheel was built without its YAML files; if this is a source "
            "checkout, the file is missing from the working tree"
        )
    try:
        return config[key]
    except (KeyError, TypeError):
        available = sorted(config) if hasattr(config, "__iter__") else []
        raise CrewConfigMissing(
            f"{path} does not define {key!r}; it defines {available}. The prompts and the code "
            "that names them have drifted apart"
        ) from None
