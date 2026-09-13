"""ReportTranslationCrew: a single-agent, single-task CrewAI crew that renders
one month's briefing prose in another language, defined in `config/agents.yaml`
and `config/tasks.yaml`.

The language, the month and the numbered blocks are not constructor state - they
are interpolated into `config/tasks.yaml`'s `{language}`, `{month}`,
`{block_count}` and `{blocks}` placeholders via `.crew().kickoff(inputs=...)`,
matching `src/agentic_portfolio/agents/summary_crew/crew.py`. Only the LLM
string is attached here in Python, since YAML cannot express it.

This crew has no tools, for the same reason its sibling has none and one more.
Everything it is allowed to know is in the blocks it is given; a tool would be a
way for it to reach past them and find a figure nobody computed. And a
translator with a search tool is a translator that can look up what a ticker
"really" means and write that instead, which is precisely the substitution the
markers exist to prevent.

`verbose=False` here for the same reason as in the summary crew: this runs
underneath a command whose entire terminal output is a briefing and a couple of
save notices, and CrewAI's progress banner would bury it.
"""

from __future__ import annotations

from crewai import Agent, Crew, Process, Task
from crewai.project import CrewBase, agent, crew, task

from agentic_portfolio.agents.crew_config import require
from agentic_portfolio.agents.translation_schema import TranslatedBriefing

_AGENTS = "agentic_portfolio/agents/translate_crew/config/agents.yaml"
_TASKS = "agentic_portfolio/agents/translate_crew/config/tasks.yaml"


@CrewBase
class ReportTranslationCrew:
    """One instance = one `translate_briefing` call, scoped to one LLM."""

    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    def __init__(self, model: str) -> None:
        self.model = model

    @agent
    def translator_agent(self) -> Agent:
        return Agent(
            config=require(self.agents_config, "translator_agent", _AGENTS),
            llm=self.model,
            verbose=False,
        )

    @task
    def translation_task(self) -> Task:
        return Task(
            config=require(self.tasks_config, "translation_task", _TASKS),
            output_pydantic=TranslatedBriefing,
        )

    @crew
    def crew(self) -> Crew:
        return Crew(
            agents=self.agents,
            tasks=self.tasks,
            process=Process.sequential,
            verbose=False,
        )
