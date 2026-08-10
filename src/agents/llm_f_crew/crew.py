"""LLMFCrew: a single-agent, single-task CrewAI crew that reads a batch of
news headlines about one ticker/month and produces a `HeadlineSentimentBatch`
(one `HeadlineSentiment` per headline), defined in `config/agents.yaml` and
`config/tasks.yaml`. The headlines text and LLM string are attached here in
Python (via `.crew().kickoff`'s `inputs`, not by subclassing), since YAML
cannot express either. `src/agents/llm_f.py`'s `generate_signal` combines
this batch into a `SentimentSignal` via `compute_decayed_score` - this crew
never produces a `SentimentSignal` itself.
"""

from __future__ import annotations

from crewai import Agent, Crew, Process, Task
from crewai.project import CrewBase, agent, crew, task

from src.agents.llm_f_schema import HeadlineSentimentBatch


@CrewBase
class LLMFCrew:
    """One instance = one `generate_signal` call, scoped to one LLM
    (`model`). `ticker`/`month`/`headlines` are not constructor state -
    they're interpolated into `config/tasks.yaml`'s `{ticker}`/`{month}`/
    `{headlines}` placeholders via `.crew().kickoff(inputs=...)`, matching
    `LLMSCrew`'s `{as_of_date}` pattern.
    """

    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    def __init__(self, model: str) -> None:
        self.model = model

    @agent
    def sentiment_agent(self) -> Agent:
        return Agent(config=self.agents_config["sentiment_agent"], llm=self.model, verbose=True)

    @task
    def sentiment_task(self) -> Task:
        return Task(config=self.tasks_config["sentiment_task"], output_pydantic=HeadlineSentimentBatch)

    @crew
    def crew(self) -> Crew:
        return Crew(agents=self.agents, tasks=self.tasks, process=Process.sequential, verbose=True)
