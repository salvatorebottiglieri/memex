---
source_url: https://arxiv.org/pdf/2606.27226
title: "Ask, Don't Judge: Binary Questions for Interpretable LLM Evaluation and Self-Improvement"
---

# BINEVAL: Ask, Don't Judge

Paper by Sangwoo Cho, Kushal Chawla et al. (Capital One, 2026-06-25). Accepted to ICML 2026 Workshop on Compositional Learning.

**Core idea:** Instead of asking an LLM for one broad judgment score, decompose evaluation criteria into atomic binary (yes/no) questions and aggregate verdicts into interpretable, multi-dimensional scores.

**Three components:**
1. Meta-prompt decomposes task prompt into atomic questions by evaluation dimension
2. Evaluator answers each question independently, aggregates into per-dimension and overall scores
3. Two-phase optimization loop (cross-model and self-update) improves prompts using question-level feedback

**Results:** Matches/outperforms UniEval and G-Eval on SummEval, Topical-Chat, and QAGS. Particularly strong on factual consistency.
