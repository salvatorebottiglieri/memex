---
source_url: https://arxiv.org/pdf/2607.05391
title: "LLM-as-a-Verifier: A General-Purpose Verification Framework"
---

# LLM-as-a-Verifier

Paper by Kwok, Li, Atreya et al. (Stanford, UC Berkeley, NVIDIA Research). Published 2026-07-06.

**Core idea:** A general-purpose verification framework that computes expectation over scoring token logits to generate continuous scores — unlike standard LM judges that produce discrete scores.

**Three scaling axes:**
1. Score granularity (more tokens → better separation)
2. Repeated evaluation (more runs → lower variance)
3. Criteria decomposition (more criteria → better accuracy)

**State-of-the-art results:** Terminal-Bench V2 (86.5%), SWE-Bench Verified (78.2%), RoboRewardBench (87.4%), MedAgentBench (73.3%)

**Also used as:** dense reward signal for RL (improves SAC and GRPO sample efficiency on robotics and math reasoning), task progress tracking proxy for Claude Code and Codex extensions.
