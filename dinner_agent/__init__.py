"""dinner_agent package entrypoint for Google ADK CLI (`adk run`, `adk web`, `adk eval`)."""
from . import agent
from .agent import DinnerPlannerAgent, root_agent

__all__ = ["agent", "root_agent", "DinnerPlannerAgent"]
