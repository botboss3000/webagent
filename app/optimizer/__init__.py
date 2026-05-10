"""
Skill Optimizer for webAgent — self-improving skill layer.

Subagents analyze real user interactions and autonomously improve
skill definitions to minimize user time, turns per task, token usage,
and message size.
"""

from app.optimizer.runner import run_optimizer_async

__all__ = ["run_optimizer_async"]
