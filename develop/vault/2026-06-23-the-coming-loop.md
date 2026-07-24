---
source_url: https://lucumr.pocoo.org/2026/6/23/the-coming-loop/
title: The Coming Loop - Armin Ronacher
---

# The Coming Loop

Armin Ronacher's reflection on the rise of **harness-level loops** in agentic coding — patterns where work is put into a queue, a machine picks it up, attempts it, stops, and a harness decides whether to continue.

**Key themes:**
- Two types of loops: the agent loop (inside every coding agent — tool calls, read/edit/test) and the harness loop (outside — orchestration across sessions)
- Loops that produce artifacts without longevity (porting code, performance exploration, security scanning) work astonishingly well
- Loops for lasting code are more concerning: they amplify defensive coding, add fallbacks instead of making bad states impossible
- The shift from software as deterministic machine to software as organism — systems we monitor and stabilize but don't fully comprehend
- Security as the clearest example where opting out is not possible — attackers will loop against your software
- Dependency on machines: codebases that assume machine participation for maintenance
- Future harnesses need better visualization, legibility, and ways to keep humans in the loop
- Pi has been cautious — and that caution is good

> "I don't prompt Claude anymore. I have loops running that prompt Claude and figuring out what to do. My job is to write loops." — Boris Cherny
