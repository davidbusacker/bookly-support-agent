# Bookly Agent Operating Policies (AOPs)

Instructions Riley follows at runtime. Edit these files to change agent behavior — not Python.

Every policy file (except this README) must start with frontmatter:

```
---
title: Short name
description: When Riley should load this policy (one or two sentences).
startup: true   # optional; default false. Full text is inlined in the system prompt.
---
```

`aop_loader.py` scans `aops/*.md` and builds:

- A **catalog** (every AOP’s title + description) in the system prompt
- Full text for `startup: true` policies
- The `read_aop` tool’s allowed `policy_id` list

Adding a policy = add a new `.md` file with frontmatter. Do not edit `core.md` or a Python ID list.

Use `read_aop` to load an on-demand policy (`startup: false`) in full when the catalog description matches the situation.
