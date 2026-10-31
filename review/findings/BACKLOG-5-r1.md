# BACKLOG-5 — Review round 1 (Reviewer: Claude Opus 5.5)

Branch `slice/backlog-5` @ `90b8b2b` (already merged with main `0281318`). Diff `main...slice/backlog-5`: MASTER_PLAN.md, configs/triage.yaml, docs/p4-implementation.md, docs/triage.md, tests/test_simulator_cli.py, tests/test_triage_runbooks.py, triage/runbooks.py. No changes to `simulator/`, `triage/gate.py`, or evaluator code. No repo or worktree file was edited during review (`git status --short` is clean after every command).

## Commands run (in the worktree)
- `UV_CACHE_DIR=/tmp/payguard-uv-cache uv run pytest -q` → **331 passed in 728.66s**, 0 xfail.
- smoke generate: `data/p1-smoke` already existed (generate refused: "output directory already contains files"), so I generated fresh into `/tmp/claude-1000/backlog5-r1/smoke`, then ran `validate --run` → **16 passed, 0 failed** (validate on the existing `data/p1-smoke` also: 16/16).
- `python3 review/check_links.py` → 16 files, 0 broken links.
- `python3 review/verify_plan_claims.py` → 19 examples verified; `review/verification_results.json` unchanged.

## Acceptance checks
| Check | Result |
|---|---|
| Insufficient never yields `close`/`data_ticket` | OK. A small-sample candidate returns `status=pending`, `evaluation_status=insufficient` (runbooks.py:665-726). The gate allows `data_ticket` only when `unit_or_null_change.status == "pass"` (gate.py:621-638), so an agent or rule proposal of `data_ticket` becomes `human_review`. A recheck is possible only through the existing recheck cap (2 rechecks, ≥24 h apart), which matches the triage.md §3 row "Hết 2 lần recheck vẫn < n_min". |
| Dense ×100 still → `data_ticket` | OK. `test_dense_median_candidate_still_routes_data_ticket` (8/8 txns). I also scanned the real smoke: `ep-0002/incident-ea21…` qr counts [2,3], ratio 115.7 → data_ticket; `ep-0004/incident-7cfa…` wallet [4,3], ratio 139.7 → data_ticket. |
| Sparse ×100 → insufficient | OK. Fixture tests for 1/1 (median path and NULL path). Smoke `ep-0001/incident-c898…` card [18,1], ratio 518.6 → pending/insufficient → human_review. |
| Changed assertions map to user decisions | OK. (1) The ep-0001 FP test lost its xfail; it now expects pending/insufficient/human_review: decision 31/10 01:45, MASTER log 31/10. (2) `_EXPECTED_SMOKE_DATA_TICKETS` went from 5 to 3 IDs (e346 and the clean FP removed): decision 31/10 02:20. (3) A `pending` branch was added in the pipeline smoke test: decision 31/10 02:40. It is guarded by `assert not window_contains_jump` and still requires human_review and not data_ticket/close. The first jump incident of each pipeline episode must still `pass` → `data_ticket`. Clean/seasonal data_ticket went from `== {FP}` to `== set()`, plus an explicit `FP not in`, which is stricter. No other assertion was loosened. |
| Locked values not deleted | OK. Targets, band, 20 points, 70% and close_enabled are unchanged. In triage.md:57-58, n_min moved out of the "Mọi ngưỡng…" bullet into its own locked bullet; the "chỉnh trên development rồi khóa" rule is kept for the rest. `policy_version` → `round2.v2` (the combined version from the 01:45 merge). |
| Truth isolation / AS-OF | OK. All inputs still come through `read_as_of(operational, …)` (runbooks.py:957-981), and no private/evaluator reads were added. Counts are computed from the same as-of transaction list already used for the medians. |
| n_min on development only, with provenance | Value and procedure OK. The documented evidence is stale: see Finding 1. |

## Findings

### F1 — n_min evidence in docs describes the pre-P1.3 world, not the current one (non-blocking; docs fix before merge)
- **Where:** docs/triage.md:57. Also configs/triage.yaml:21-28, whose `reference` points to it.
- **Claim in docs:** on ep-0001 (development), "qr có 237 ngày có giao dịch, median 3, 30 ngày singleton; card có 37 ngày, ít nhất 8 giao dịch/ngày".
- **Counterexample:** counted base+ep-0001 rows per local day and channel (segment `all`, Asia/Ho_Chi_Minh), same as the runbook does.
  - Current branch world (`/tmp/claude-1000/backlog5-r1/smoke`):
    `card days 138 median 1.5 min 1 singletons 69` · `qr days 196 median 2.0 singletons 93` · `wallet days 184 median 2.0 singletons 71`
  - Pre-P1.3 main `07c90ec` (`git archive eb373c1^1`, smoke regenerated into `/tmp/claude-1000/backlog5-r1/oldsmoke`):
    `card days 37 median 9 min 8 singletons 0` · `qr days 237 median 3 min 1 singletons 30`. This matches the docs exactly.
  - So the numbers were measured on the world before the P1.3 merge. The current world has 3 channels, and card days are now ~50% singletons, not "≥8/day". The p4-implementation.md note on the FP (card 18,1,1,0,2,0,1) already contradicts triage.md:57 on the same branch.
- **Impact:** the rule "smallest integer that excludes singletons" gives `n_min = 2` in both worlds, so neither the value nor the code changes. I found no evidence that calibration or test was used: ep-0001 is `development` in both manifests. But the provenance of a locked value cites figures that are false for the data it governs. In the current world n_min also binds on about half of all channel-days, which explains the always-recheck rise of 30 → 58 calls. Docs should say so.
- **Proposed fix (docs only, can be done by the orchestrator):** replace the figures in triage.md:57 with the current development counts (card 138/69 singletons, qr 196/93, wallet 184/71; medians 1.5/2/2). Add one line that the value was first chosen on the pre-P1.3 world and re-checked after the P1.3 merge with the same result. Keep `n_min = 2` and the lock.

### F2 — the n_min count includes NULL rows, so a median computed from 1 non-null value can still pass (non-blocking)
- **Where:** triage/runbooks.py:283 (`transaction_count = len(amounts)`, NULLs included), used at :358. The median is computed only over non-null values (:364).
- **Counterexample:** `uv run python /tmp/claude-1000/backlog5-r1/probe/probe_null_singleton.py` (fixture `_fixture`, pre and post days with 3 rows each, 2 of them NULL, non-null 10,000 → 1,000,000):
  ```
  nonnull_singleton_both_days(3 rows, 2 NULL each) status= pass ratio= 100.0 null_inc= 0.0 counts= [... 3, ... 3] gate= data_ticket
  ```
  The NULL rate does not change, so the data_ticket rests only on a median ratio from n=1 vs n=1. That is exactly the singleton case BACKLOG-5 is meant to stop.
- **Impact:** this is a literal implementation of triage.md:48-49 ("số giao dịch"), so code and docs agree. It needs an already high NULL rate before the change, which the current smoke data never reaches: the smoke scan shows no such case. Not an MVP blocker.
- **Proposed fix (round 2 or backlog, user decides):** for the median path, apply n_min to the non-null count of each day. For the NULL path, keep the total count. Clarify this in triage.md §3 in the same change, and add a fixture test like the one above (expected pending/insufficient).

### Observations (no action required)
- The real pipeline jump transitions pass at qr [2,3] and wallet [4,3]. `n_min = 3` would have turned `ep-0002/incident-ea21…` (calibration) into insufficient. The chosen value is the structural minimum, not a value fitted to keep these cases. Any future change to n_min must be re-selected on development only, as the docs already say.
- docs/orchestration.md:138 (run log 30/10 22:45) still says "policy v3". The final config is `round2.v2`. This is a historical log row; the orchestrator can leave it or annotate it.

## Verdict
The behaviour is correct and the acceptance tests pass (331). Every changed expectation maps to the user decisions of 31/10 01:45, 02:20 and 02:40. Invariants are intact. F1 is a docs correction the orchestrator can make while merging. F2 is a backlog item.

VERDICT: PASS
