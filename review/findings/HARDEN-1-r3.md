# HARDEN-1 — Review vòng 3 (cuối, người dùng duyệt thêm)

Reviewer: Claude Opus 5.5 (sub-agent). Nhánh `slice/harden-1` @ `629d039` (commit `ba3b502` + merge main `74ddde2`), worktree `/home/lieu_hoai_phuc/.herdr/worktrees/Aiagent/slice-harden-1`. Không sửa file nào trong repo/worktree (`git status` sạch trước và sau). Mọi script, state, DB, log nằm dưới `/tmp/claude-1000/harden1-r3/`. Không gọi LLM API thật; harness chỉ dùng fake provider và `PAYGUARD_BENCHMARK_STATE_DIR` tạm.

Phạm vi vòng 3 do người dùng chốt: sửa tối thiểu, `model evaluate` từ chối calibration/test trước khi đọc truth, ghi report hay chạm ledger, cho tới khi artifact được bind hash (HARDEN-2); khôi phục ca regression support cũ song song với ca mới.

## Kết quả chính

| Mục | Kết quả |
|---|---|
| R2-1 (chặn) | **Đóng bằng refusal.** Đã chạy lại phản ví dụ của reviewer r2 (bản copy artifact, threshold fraud đổi `0.29236…` → `1.0`, version giữ nguyên) với `--episode-role test`, `calibration`, `development+test`, `calibration+test`. Cả 4 đều exit 2 với lỗi "held-out model evaluation is disabled…". Truth DB không được mở, không có report, state/ledger giữ nguyên byte. Artifact gốc với test cũng bị từ chối. Development vẫn chạy. |
| R2-2 (không chặn) | **Đóng.** Test parametrize gồm cả ca cũ (checklist `error`, không marker) và ca mới (runner error, checklist pass). Mutation probe cho thấy cả hai ca đều kiểm thật. |
| Hồi quy r1/r2 | Không thấy hồi quy: A2, A3, B1, B2, C1, N1–N4 và các probe config R1-1 đều đạt (chi tiết bên dưới). |
| Full suite (chạy đúng 1 lần) | **422 passed, 1 xfailed**, exit 0, 717 s. |
| Smoke / links / verifier | Generate exit 0, fingerprint `6defebb0…caf244` (giống r2/main); validate 16/16; check_links 16 file, 0 link hỏng; verify_plan_claims 19 ví dụ, `review/verification_results.json` không đổi. |

## Kiểm tra chi tiết

### (1) R2-1: refusal trước truth/report/ledger

Code: `model/__main__.py:107-132`. Lệnh đọc role từ operational DB (read-only), rồi từ chối `calibration`/`test` ở dòng 125-132. Bước này chạy trước `find_harness`/`verify_harness_policy_run`/`assert_role_access`, trước khi mở `--private-db` và trước `evaluate_model_scores`.

Probe trong process: `/tmp/claude-1000/harden1-r3/probe_model_r3.py`, log ở `probe_model_r3.log`.
- Fixture: harness thật (BASE/policy copy từ `/tmp/claude-1000/audit/h1`), có prompt-rule lock và policy lock.
- Cách đo: spy trên `sqlite3.connect` ghi mọi DB được mở, so SHA-256 toàn bộ state dir trước/sau, kiểm sự tồn tại của `model/reports/evaluation.json`.

| variant | roles | rc | truth mở | report | state |
|---|---|---|---|---|---|
| changed (thr 1.0) | test | 2 | không | không | không đổi |
| changed | calibration | 2 | không | không | không đổi |
| changed | development,test | 2 | không | không | không đổi |
| changed | calibration,test | 2 | không | không | không đổi |
| original | test | 2 | không | không | không đổi |
| changed | (mặc định) | 0 | có | có | ghi một entry development (đúng thiết kế) |
| changed | development | 0 | có | có | không đổi (entry đã có, idempotent) |

Ở các ca bị từ chối, DB duy nhất được mở là `policies/rule_agent_fake/operational/observations.sqlite?mode=ro`. Sau probe, `verify_harness_policy_run(..., ('test',))` vẫn PASS.

CLI công khai, đúng dạng lệnh của r2:
```
PAYGUARD_BENCHMARK_STATE_DIR=/tmp/claude-1000/harden1-r3/state uv run python -m model evaluate --run-id changed --data-root /tmp/claude-1000/harden1-r3/model-inputs --operational-db …/policies/rule_agent_fake/operational/observations.sqlite --private-db …/base/private/evaluator.sqlite --episode-role test   # và --episode-role calibration
```
Kết quả: cả hai lần đều `ERROR: held-out model evaluation is disabled until model artifacts are content-hash bound into the frozen identity; follow-up is required before pilot`, rc=2. `sha256sum` của state dir trùng trước/sau (STATE_UNCHANGED). Trong `model-inputs/changed/model/reports/` chỉ có `training_audit.json`, không có `evaluation.json`.

Khi không truyền role flag, selection chỉ gồm development (`model/__main__.py:121-124`; `model/evaluate.py:392-395` khi `episode_ids=None`). Trong harness, relabel role ở policy copy bị chặn bởi fingerprint BASE: `RunIdentityError: policy 'rule_agent_fake' input fingerprint differs from frozen BASE` (`probe_relabel.py`). Run standalone xem ghi chú NB-1.

Test functional mới `tests/test_model_scores.py:433-554` parametrize `("test",)`, `("calibration",)`, `("development","test")`. Test dùng bản copy `--data-root/--run-id` với threshold fraud đổi, và assert: không mở private DB, không gọi `assert_role_access`, không gọi `evaluate_model_scores`, không có report, ledger giữ nguyên byte. `test_smoke_cli_scores_episode_asof_and_evaluates_private_truth` có thêm assert từ chối `--episode-role test` trên smoke thật.

### (2) R2-2: hai đầu vào regression

`tests/test_evaluation_support.py:456-516` parametrize `["checklist-error", "runner-error"]`:
- Ca `checklist-error` khớp đúng đầu vào cũ trên main (`agent_run_sql.status='error'`, assert không có `__runner_guard`).
- Ca `runner-error` có checklist pass và marker runner.
- Cả hai assert UQ (1)–(3)=0 và `off`=0.

Để kiểm test có bắt lỗi thật, tôi chạy mutation `/tmp/claude-1000/harden1-r3/mut/test_mut_support.py`: monkeypatch `evaluation.support._has_forced_human_condition` thành `False`. Kết quả: `FAILED test_mutant[checklist-error] - assert 1 == 0`, `FAILED test_mutant[runner-error] - assert 1 == 0`; hai ca thật vẫn pass. Không ca nào bị bỏ.

### (3) Hồi quy các mục đã đóng ở r1/r2

Phần end-to-end dùng bản clone git của nhánh tại `/tmp/claude-1000/harden1-r3/clone`, chạy harness CLI với fake provider và state tạm:
- **A2 (lock trên code đã đổi):**
  - Chạy run với code A → lock → `evaluation decisions --episode-role test` rc 0. Trước khi lock, lệnh này rc 2 với lỗi "required lock marker is missing".
  - Commit đổi `triage/gate.py` (code B). Sau đó `evaluation decisions` test/calibration, `payguard run` resume và `triage run --episode-role test` đều rc 2 với `hashes differ: code_hash`; seed-role ledger giữ nguyên byte. Lock lại với code B bị từ chối (lock bất biến).
  - Chuỗi A2 chuẩn: root mới chạy code B, commit code C, lock C trên state mới, rồi evaluate test, calibration, mặc định, development và support. Tất cả rc 2 trước truth.
- **B1:** commit chỉ sửa docs (`docs/model.md`), sau đó `payguard run` resume rc 0, và `evaluation decisions --episode-role test` vẫn rc 0.
- **A3:** `model evaluate` mặc định chỉ chạy `ep-0001`. Probe `probe_configs.py` (bản copy từ r2): 7 ca run/measure với config triage/simulator/LLM tùy chỉnh đều rc 2; DB, state và report giữ nguyên. Run test có lock dùng config mặc định rc 0, cho 17 decision chỉ thuộc test. Các test `test_p4_cli_honors_episode_role_and_defaults_to_development` và `test_heldout_harness_triage_waits_for_lock_and_stale_run_cannot_be_scored` pass trong full suite.
- **B2:** `test_lock_profile_must_match_root_and_pilot_maps_to_benchmark` và `test_standalone_openrouter_triage_uses_durable_benchmark_ledger` pass trong full suite.
- **C1:** import `triage.__main__/runner/gate/agent/tools/measure` và `payguard.run_identity` trong process mới: không có module `evaluation*` trong `sys.modules`, `payguard.harness` không được nạp; grep không thấy `from evaluation` trong `triage/`.
- **N1 (`probe_scope.py`):** same_start on=0, different_start on=0, future_rule on=1; off=1 và reason=null ở cả ba. Giống r2.
- **N2/N3:** các test `by_truth_group` và null-reason pass trong full suite.
- **N4 (`probe_runner_guard.py`):** marker spoof từ provider bị bỏ (`MARKER None`, close). Khi `tool_error=true`, code ghi marker `forced_human/runner_tool_error`. Checklist error gốc vẫn cho UQ off/on=0.

### (4) Bypass mới, docs, test, giá trị khóa

- Không thấy đường nào khác dùng artifact model để đọc truth held-out. `payguard/harness.py` chỉ import `score_monitoring_episodes`/`train_operational_model`, và `evaluate_model_scores` chỉ được gọi từ `model/__main__.py:192`. Các CLI đọc truth khác (`evaluation decisions/support`) đã đi qua guard lock/run-manifest.
- Docs khớp code:
  - `docs/model.md:52` và `docs/evaluation.md:107` mô tả đúng việc tạm tắt held-out, rằng development vẫn chạy, và follow-up trước pilot. HARDEN-2 đã có spec trong `docs/p5-implementation.md`.
  - `docs/data.md` §9–10 (`__runner_guard`, lock profile chuẩn hóa, resume không bị Git metadata chặn, cột reason `by_close_enabled`/`by_truth_group`) khớp hành vi đã probe.
- Test không bị nới lỏng. Assert duy nhất bị đổi (`sample_count == score_count` → tổng score development + danh sách episode + `pr_auc` trong [0,1]) phản ánh thay đổi có chủ đích "mặc định development" và chặt hơn bản cũ. Ca support cũ đã quay lại.
- Diff không chạm `configs/`, `MASTER_PLAN.md`, `review/verify_plan_claims.py`. Fingerprint smoke không đổi.

## Findings

Không có finding chặn MVP.

### NB-1 — không chặn: run standalone tin `episodes.role` ở operational DB

- **Vị trí:** `model/__main__.py:107-124` (tương tự `model/evaluate.py:383-395`).
- **Phản ví dụ:** `probe_relabel.py`, log ở `probe_relabel.log`. Trên bản copy standalone (không có harness manifest), drop trigger bất biến của bảng `episodes`, rồi đổi `ep-0004` (test) thành `development`. `model evaluate` không role flag cho rc 0 và chấm `['ep-0001', 'ep-0004']`, tức là đọc truth của một episode test.
- **Mức độ:**
  - Muốn làm vậy phải cố ý sửa DB và gỡ trigger `episode roles are locked and immutable`.
  - Chỉ xảy ra ngoài harness. Trong harness, fingerprint BASE đã chặn (rc 2).
  - Không phải hồi quy: trước HARDEN-1, standalone chấm mọi role.
- **Đề xuất (HARDEN-2):** đối chiếu role đã chọn với `episode_manifest.role` trong private store (hoặc manifest đã freeze) trước khi đọc bảng truth, và từ chối khi lệch.

### NB-2 — không chặn, thẩm mỹ: nhánh chết sau refusal

- **Vị trí:** `model/__main__.py:144` và `152-158`.
- **Chi tiết:** điều kiện `any(role != "development" …)` và lỗi "outside a P5.1 harness is limited to development" không còn đường nào chạy tới, vì refusal ở dòng 125-132 đã chặn mọi held-out role. Không có hại; chỉ là phần giữ chỗ cho HARDEN-2.
- **Đề xuất:** khi HARDEN-2 mở lại held-out, giữ hoặc xóa có chủ đích và thêm test cho nhánh này.

## Lệnh và artifact

- Full suite (một lần): `PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/tmp/payguard-uv-cache UV_NO_SYNC=1 PAYGUARD_BENCHMARK_STATE_DIR=/tmp/claude-1000/harden1-r3/suite-state uv run pytest -q --durations=10 -o cache_dir=… --basetemp=…`. Kết quả 422 passed, 1 xfailed (xfail seasonal strict đã biết), log ở `full-suite.log`.
- Smoke: `uv run python -m simulator generate --profile smoke --out /tmp/claude-1000/harden1-r3/smoke`, rồi `validate`: 16 passed / 0 failed.
- `python3 review/check_links.py`: 16 file, 0 link hỏng. `python3 review/verify_plan_claims.py`: 19 ví dụ; worktree vẫn sạch nên không cần restore.
- Probe: `probe_model_r3.py`, `probe_relabel.py`, `probe_configs.py`, `probe_scope.py`, `probe_runner_guard.py`, `mut/test_mut_support.py`, clone e2e `clone/`, state `e2e-state*`. Tất cả nằm dưới `/tmp/claude-1000/harden1-r3/`.

VERDICT: PASS
