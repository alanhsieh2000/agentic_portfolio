"""ReportSummaryCrew: a single-agent, single-task CrewAI crew that turns one
month's already-computed portfolio figures into the prose of a briefing,
defined in `config/agents.yaml` and `config/tasks.yaml`.

The month, the report count and the computed facts are not constructor state -
they are interpolated into `config/tasks.yaml`'s `{month}`, `{report_count}`
and `{facts}` placeholders via `.crew().kickoff(inputs=...)`, matching
`src/agentic_portfolio/agents/llm_f_crew/crew.py`'s `{ticker}`/`{month}`/`{headlines}` pattern.
Only the LLM string is attached here in Python, since YAML cannot express it.

This crew has no tools, deliberately. Everything it is allowed to know is in
the facts string it is given; a tool would be a way for it to reach past that
and find a number nobody computed for it.
"""

from __future__ import annotations

from crewai import Agent, Crew, Process, Task
from crewai.project import CrewBase, agent, crew, task

from agentic_portfolio.agents.summary_schema import MonthNarrative


@CrewBase
class ReportSummaryCrew:
    """One instance = one `generate_narrative` call, scoped to one LLM
    (`model`).

    `verbose=False` here, where both other crews in this project use
    `verbose=True`, because this one runs underneath a command whose entire
    terminal output is a briefing and a one-line save notice. CrewAI's
    progress banner would bury the thing the user asked for. LLM-S and LLM-F
    run inside long pipelines where that banner is the only sign of progress,
    so the difference is in the setting, not in a change of mind.
    """

    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    def __init__(self, model: str) -> None:
        self.model = model

    @agent
    def summary_agent(self) -> Agent:
        return Agent(config=self.agents_config["summary_agent"], llm=self.model, verbose=False)

    @task
    def summary_task(self) -> Task:
        return Task(config=self.tasks_config["summary_task"], output_pydantic=MonthNarrative)

    @crew
    def crew(self) -> Crew:
        return Crew(agents=self.agents, tasks=self.tasks, process=Process.sequential, verbose=False)
