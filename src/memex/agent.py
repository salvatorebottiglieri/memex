"""Agent seam — abstract base class and plugin loader.

Implementations live in :mod:`memex.derivers` and are loaded via the
``MEMEX_AGENT`` environment variable (``module:Class`` syntax).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from memex.schemas import DerivationResult, ReviewProposal


class Agent(ABC):
    """Abstract base for all agents.

    Implementations must provide at minimum ``derive()``, ``review()``, and
    ``extract_ideas()``. Optional overrides for ``generate_title()``.

    Subclasses are loaded via :func:`load_agent` using ``MEMEX_AGENT`` env var.
    """

    @abstractmethod
    def derive(self, content: str) -> DerivationResult:
        ...

    @abstractmethod
    def review(
        self,
        target_content: str,
        asserting_content: str,
        edge_payload: dict,
    ) -> ReviewProposal:
        ...

    def generate_title(self, content: str, url: str) -> str | None:
        """Infer a human-readable title from content and URL. Return None to skip."""
        return None

    def extract_ideas(self, content: str, source_url: str | None = None) -> list[str]:
        """Extract 3-5 key ideas from content. Return empty list by default.

        ``source_url`` is advisory (for context) — implementations may use or ignore it.
        """
        return []


def _verify_agent_methods(client: object, module_path: str) -> None:
    """Verify the loaded agent instance has derive, review, and extract_ideas callables.

    Raises ImportError if any method is missing or not callable.
    """
    for method_name in ("derive", "review", "extract_ideas"):
        if not hasattr(client, method_name) or not callable(getattr(client, method_name)):
            raise ImportError(
                f"Agent '{module_path}' is missing required method '{method_name}'. "
                f"Loaded agent class must implement derive(), review(), and extract_ideas()."
            )


def load_agent(module_path: str | None = None) -> Agent:
    """Load an agent from a ``module:Class`` string, or return DemoAgent()."""
    if not module_path:
        from memex.derivers.demo import DemoAgent

        return DemoAgent()
    from memex.plugin import load_class

    client = load_class(module_path)
    _verify_agent_methods(client, module_path)
    return client
