---
source_url: https://martinfowler.com/articles/reliable-llm-bayer.html
title: Building Reliable Agentic AI Systems — Bayer/Thoughtworks case study
---

# Building Reliable Agentic AI Systems

A case study from Bayer AG and Thoughtworks on building the Preclinical Information Center (PRINCE), a production-ready agentic RAG system for pharmaceutical drug development.

**Architecture:** LangGraph-based multi-agent orchestration with Researcher, Writer, and Reflection agents. Uses Agentic RAG + Text-to-SQL over decades of safety study reports.

**Key engineering decisions through context engineering and harness engineering:**
- Context discipline: different stages receive different context (planning, retrieval, evidence, synthesis) — not one large container
- Clarify User Intent as first defense against ambiguity
- Resilience: retries at both LLM call level and logical node level; fallback models/platforms
- Observability: Cloudwatch + Langfuse for traces and evaluation (RAGAS framework)
- Human-in-the-loop for governance and compliance
- Evolution through three phases: Search → Ask → Do

**Published:** 16 June 2026 on Martin Fowler's blog.
