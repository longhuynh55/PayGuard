# PayGuard docs — reader's map

All design docs in this folder are written in **Vietnamese**. The English summary of the whole
project is the [main README](../README.md); this page tells you what each file covers so you can
pick the right one (a browser translator works well on them).

Rule used everywhere: each topic lives in **one** file, and other files link to it. Data schemas
are defined only in [data.md](data.md).

## Design (what the system does and why)

| File | What it covers | English summary in the main README |
|---|---|---|
| [overview.md](overview.md) | The business problem (alert fatigue, late labels), the layered solution, constraints, architecture, risks | [The problem](../README.md#the-problem), [How it works](../README.md#how-it-works) |
| [data.md](data.md) | Simulator, as-of schema for every table, the 4 label states, business calendar, episodes and their split, the private truth table, report formats. **Owns all data schemas.** | [How the test is kept fair](../README.md#how-the-test-is-kept-fair), Technical details → Episodes |
| [model.md](model.md) | Time-based splits, LightGBM, isotonic calibration, Mondrian conformal sets on the raw score | Technical details → Fraud model |
| [monitoring.md](monitoring.md) | Metric → signal → alert → incident, merge window, child incidents, thresholds and how they were chosen | Technical details → Monitoring |
| [triage.md](triage.md) | Cause taxonomy, `evidence_status`, the gate's action branches, the 4 runbook checklists, close permissions, the agent, the `run_sql` tool, the `decisions` table | [A worked example](../README.md#a-worked-example), Technical details → Triage |
| [evaluation.md](evaluation.md) | Episode roles, matching incidents to truth, scoring at `decision_at`, metrics, baselines, statistics, evaluator CLI | Technical details → Evaluation |
| [rationale.md](rationale.md) | Positioning, research findings behind each choice, LLM choice, reference list | [Design decisions](../README.md#design-decisions), [Related work](../README.md#related-work) |
| [app.md](app.md) | API, dashboard, Docker and demo (placeholder until stage P6) | — |

## Build plans (how each stage was implemented)

These sub-plans hold task order, files, checks and acceptance criteria for each stage. They do
not repeat the contracts above.

| File | Stage |
|---|---|
| [p1-implementation.md](p1-implementation.md) | P1 — simulator and incident injectors |
| [p2-implementation.md](p2-implementation.md) | P2 — fraud model |
| [p3-implementation.md](p3-implementation.md) | P3 — monitoring |
| [p4-implementation.md](p4-implementation.md) | P4 — triage (rules, gate, agent) |
| [p5-implementation.md](p5-implementation.md) | P5 — evaluation prep: harness, cost ledger, matcher, development support tables and diagnosis notes |

## Process

| File | What it covers |
|---|---|
| [orchestration.md](orchestration.md) | How the AI agents (planner, implementers, reviewer) are coordinated, stop rules, run log |
| [../MASTER_PLAN.md](../MASTER_PLAN.md) | Current status, roadmap, MVP scope, LLM budget, decision log |
| [../AGENTS.md](../AGENTS.md) | Rules and invariants every agent must follow |
