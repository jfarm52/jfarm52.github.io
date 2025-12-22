# jfarm52.github.io
Data collectionz
# Carlisle Energy Data Collection – Agent Rules

## AGENT INSTRUCTION — DUPLICATE INPUT HANDLING
(This rule overrides default agent behavior.)
If the user sends multiple messages containing overlapping or repeated task lists, instructions, or commands:

- Treat them as a single continuous instruction set.
- Do NOT reset progress.
- Do NOT duplicate work.
- Do NOT change course unless the user explicitly says "override", "replace", or "ignore previous".
- Always assume later messages may be continuations or accidental re-sends caused by UI truncation.

Default behavior: continue current execution plan.