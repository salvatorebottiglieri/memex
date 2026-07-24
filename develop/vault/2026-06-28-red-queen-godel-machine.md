---
source_url: https://arxiv.org/abs/2606.26294
title: "The Red Queen Gödel Machine: Co-Evolving Agents and Their Evaluators"
---

# The Red Queen Gödel Machine (RQGM)

An evolutionary framework for recursive self-improvement under non-stationary utilities. ArXiv paper by Iacob, Jovanović, Shen et al. (2026-06-24).

**Key insight:** Self-improving agents generally assume stationary evaluation criteria (fixed verifier/benchmark). RQGM makes evaluation part of the improvement loop — co-evolving agents and evaluators.

**Method:** Search organized into epochs with fixed within-epoch evaluation criterion; utility updates at epoch boundaries.

**Results:**
- On verifiable coding tasks: improves test pass rate over prior SOTA with 1.35x-1.72x fewer tokens
- On scientific paper writing/reviewing: co-evolved writers reach 1.78x-1.86x higher acceptance rates
- On Olympiad-level proof writing: 9% higher ground-truth accuracy
- Corrects reviewer over-acceptance of AI-generated papers (1.91x human rate) via adversarial objective
