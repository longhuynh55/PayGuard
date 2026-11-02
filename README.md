# PayGuard

**Uncertainty-aware triage of fraud-model drift alerts when labels arrive late.**

PayGuard is a research-style portfolio project. It asks one question:

> Does a hybrid of code-based runbooks and an LLM investigation agent with uncertainty
> quantification resolve fraud-model drift alerts better than rules alone — for which
> incident types, and at what cost?

It answers that question on a synthetic payment world where every incident has a known
ground truth, so that both correct closures and missed incidents can be counted honestly.

> **Status (Nov 2026): work in progress.** The data → model → monitoring → triage →
> evaluation pipeline runs end to end on a small smoke profile, and the 60-episode pilot
> benchmark is generated and validated. **No benchmark results are published yet**; the
> results section below will be filled in only from locked, held-out runs.

---

## The problem

- A production fraud model raises many drift alerts. Most are harmless: seasonality,
  promotions, a large new merchant, a data-pipeline unit change.
- Fraud labels arrive **30–90 days late**, so an alert cannot be checked against outcomes
  when it fires.
- Investigating one alert takes an analyst hours of SQL and judgement. The result is alert
  fatigue, and real fraud shifts get lost in the noise.

Drift detectors (PSI, KS) say *that* something changed. PayGuard works on the next step:
a cause hypothesis, an evidence level computed by code, and a routing decision — close,
recheck later, or route to the right team.

## Approach

| Layer | What it does |
|---|---|
| **Simulator** (`simulator/`) | Generates a payment world (customers, merchants, QR / e-wallet / card channels, refunds, account-to-account transfers) with as-of timestamps, delayed labels in four states, a business calendar, and injected incidents: fraud surge, new fraud via mule accounts, account takeover, data-pipeline errors, seasonal and business spikes, new sparse merchants, silent concept drift, and two-cause episodes. |
| **Fraud model** (`model/`) | As-of features, LightGBM, isotonic calibration, and Mondrian conformal prediction on raw scores; uncertain transactions go to human review. |
| **Monitoring** (`monitoring/`) | Daily metrics, signals and alerts (PSI, volume against a robust baseline), grouped into incidents with child incidents after decisions. |
| **Triage** (`triage/`) | Four code-based runbooks with a gate and separate closing authority for rules and agent. An LLM agent investigates the rest with a sandboxed, as-of-scoped `run_sql` tool. Agent uncertainty is measured by self-consistency (K = N = 3); disagreement or insufficient evidence escalates to a human. |
| **Evaluation** (`evaluation/`) | The only module that reads ground truth. Matches incidents to truth (matched / ambiguous / unmatched), scores decisions, and reports support. |
| **Harness** (`payguard/`) | One command per policy: data → model → monitoring → alerts → triage → evaluator report, with run identity hashes, role locks, and a persistent cost ledger with a hard USD cap. |

## Design invariants

These are enforced by code and tests, not by convention:

- **As-of everywhere.** Features and agent SQL only see rows with `available_at <= as_of_time` in the right episode.
- **Ground truth is isolated.** Truth and injector parameters live in a private store that no agent tool can reach; only the evaluator reads it.
- **Pending is not a negative label.**
- **No tuning on test.** The episode manifest (development / calibration / test = 12 / 24 / 24) is locked before tuning; calibration and test analysis require a recorded lock.
- **Evidence status is computed by code**, never filled in by the LLM.
- **Every error rate is reported with its denominator and an uncertainty interval**; a pilot is never presented as a near-zero-risk guarantee.

## Quickstart

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync

# Generate and validate the smoke profile (5 episodes, a few minutes)
uv run python -m simulator generate --profile smoke --out data/smoke
uv run python -m simulator validate --run data/smoke

# Run the end-to-end harness with the rule-only policy (no LLM calls)
export PAYGUARD_BENCHMARK_STATE_DIR="$(mktemp -d)"
uv run python -m payguard run --profile smoke --policy rule_human --out data/bench-smoke

# Tests
uv run pytest -q
```

Policies that call a real LLM (`rule_agent_openrouter_k1`) need an `OPENROUTER_API_KEY` in
`.env` and stay under the cost cap recorded in the ledger. The benchmark budget is locked at
**USD 10** for all baselines and ablations.

## Repository layout

```
simulator/    synthetic world, injectors, episodes, validation, support reports
model/        as-of features, LightGBM, calibration, conformal, scoring
monitoring/   daily metrics, signals, alerts, incidents
triage/       runbooks, gate, run_sql tool, LLM agent, measurement
evaluation/   truth matching, decision scoring, support tables
payguard/     benchmark harness, run identity, locks
configs/      YAML configuration (simulator, episodes, model, triage, LLM)
docs/         design documents (one topic per file, see below)
tests/        functional tests (≈450)
review/       protocol checks (verify_plan_claims.py, check_links.py)
```

## Documentation

The design is documented in Vietnamese, one topic per file:

- [MASTER_PLAN.md](MASTER_PLAN.md) — status, roadmap, MVP scope, decision log
- [docs/overview.md](docs/overview.md) — problem, solution, architecture, risks
- [docs/data.md](docs/data.md) — simulator, as-of schema, labels, episodes, truth tables (owns every data schema)
- [docs/model.md](docs/model.md) · [docs/monitoring.md](docs/monitoring.md) · [docs/triage.md](docs/triage.md) · [docs/evaluation.md](docs/evaluation.md)
- [docs/rationale.md](docs/rationale.md) — research notes, design rationale, references

## Results

*Pending.* The comparison of four baselines and two agent variants (utility, workload,
missed-incident risk, closing errors and cost per incident, with episode-level bootstrap
intervals) will be reported here after the calibration and test runs are locked —
including negative results if the agent does not beat rules alone.

What has been measured so far is cost only: on two development/calibration incidents,
Qwen 3.7 Flash via OpenRouter used about 3.5k tokens per call, which extrapolates to roughly
**USD 0.0075 per agent-routed incident** for the full design. This is an extrapolation, not
a benchmark result.

## Limitations

- The data is **synthetic**. The simulator is what makes exact ground truth possible, but
  conclusions hold for the simulated world, not for any real payment network.
- 60 episodes is a pilot-sized benchmark; intervals will be wide.
- The agent's closing authority is deliberately narrow: it may only close inside existing
  benign runbooks.
- Not yet built: FastAPI service, Streamlit dashboard, Docker Compose demo, CI.

## How it was built

Development used an AI-assisted multi-agent workflow: a planner/orchestrator writes task
specs with testable acceptance criteria, implementer agents write code in isolated git
worktrees, and a fresh reviewer agent checks each change against the specs and invariants
before merge. The coordination log, including each review outcome, is in
[docs/orchestration.md](docs/orchestration.md).
