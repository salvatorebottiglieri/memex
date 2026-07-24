---
source_url: https://arxiv.org/pdf/2511.00592
title: "Agentic Auto-Scheduling: An Experimental Study of LLM-Guided Loop Optimization"
---

# ComPilot: Agentic Auto-Scheduling

Paper by Merouani, Bernou, and Baghdadi (NYU Abu Dhabi). Published at PACT 2025.

**Core idea:** Off-the-shelf LLMs guide loop optimization through closed-loop interaction with a compiler (Tiramisu). No fine-tuning needed.

**How it works:** LLM proposes loop transformations → compiler checks legality → reports speedup/slowdown → LLM refines strategy using feedback.

**Results on PolyBench:**
- Geometric mean speedups: 2.66x (single run), 3.54x (best-of-5)
- Competitive with state-of-the-art Pluto polyhedral optimizer
- Some benchmarks achieve 100x+ speedups

**Transformations supported:** Loop Fusion, Shifting, Interchange, Parallelization, 2D/3D Tiling, Unrolling, Skewing, Reversal.
