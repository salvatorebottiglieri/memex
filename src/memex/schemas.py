"""Shared domain types for the agent seam."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ReviewProposal:
    """The result of an LLM review — which nodes are affected by a changed claim."""

    affected_node_ids: list[str]
    damage_boundary_node_id: str | None
    rationale_md: str
    confidence: str  # "high" | "medium" | "low"


@dataclass
class DerivationResult:
    """The result of an LLM derivation."""

    prose: str  # Markdown prose with optional > Synthesis: markers
    synthesis_statements: list[str] = field(default_factory=list)


@dataclass
class DocumentRef:
    """A reference to a source document, handed to agents that can read files.

    Reader agents (``can_read_files=True``) receive this instead of the
    inlined content: they read the file themselves in multiple passes with
    the read tool, so arbitrarily long sources fit without a prompt cap.
    """

    node_id: str
    content_path: str
    title: str | None = None
    source_url: str | None = None
    size_bytes: int = 0
