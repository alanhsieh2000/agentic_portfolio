"""LLMSCrew: a single-agent, single-task CrewAI crew wrapping the paper's
(arXiv:2603.23300v1, Appendix C.5) verbatim `strategy_agent`/`strategy_task`
pair, defined in `config/agents.yaml` and `config/tasks.yaml`. The 4
exploration tools and the LLM string are attached here in Python, since
YAML cannot express either (see plans/02_llm_s_agent.md's Plan of Work).
"""

from __future__ import annotations

from datetime import date

import pandas as pd
from crewai import Agent, Crew, Process, Task
from crewai.project import CrewBase, agent, crew, task

from src.agents.llm_s_crew.tools import (
    GetDatabaseSchemaTool,
    GetExtremeFirmsTool,
    QueryFirmDatabaseTool,
    TestComplexConditionTool,
)
from src.agents.llm_s_schema import ScreeningRule


@CrewBase
class LLMSCrew:
    """One instance = one `generate_rule` call, scoped to one causally-
    masked snapshot (`snapshot`, `as_of_date`) and one LLM (`model`).
    """

    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    def __init__(self, snapshot: pd.DataFrame, as_of_date: date, model: str) -> None:
        self.snapshot = snapshot
        self.as_of_date = as_of_date
        self.model = model

    @agent
    def strategy_agent(self) -> Agent:
        return Agent(config=self.agents_config["strategy_agent"], llm=self.model, verbose=True)

    @task
    def strategy_task(self) -> Task:
        return Task(
            config=self.tasks_config["strategy_task"],
            tools=[
                GetDatabaseSchemaTool(snapshot=self.snapshot, as_of_date=self.as_of_date),
                QueryFirmDatabaseTool(snapshot=self.snapshot),
                GetExtremeFirmsTool(snapshot=self.snapshot),
                TestComplexConditionTool(snapshot=self.snapshot),
            ],
            output_pydantic=ScreeningRule,
        )

    @crew
    def crew(self) -> Crew:
        return Crew(agents=self.agents, tasks=self.tasks, process=Process.sequential, verbose=True)
