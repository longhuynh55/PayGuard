# HARDEN-1 — Independent review round 2

**VERDICT: CHANGES_REQUIRED.** Một lỗi chặn HARDEN-1/pilot còn lại ở effective model identity; thêm một mất mát coverage hồi quy không chặn runtime. Đây là vòng review cuối: dừng, chờ người dùng quyết định; không tự giao vòng sửa/review tiếp.

Reviewer chỉ đọc worktree `/home/lieu_hoai_phuc/.herdr/worktrees/Aiagent/slice-harden-1`, review toàn bộ diff chưa commit so với HEAD và untracked `payguard/run_identity.py`. Không sửa repo, Git, docs, tests hay state benchmark thật; không gọi API thật. Script/cache/DB/report/log chỉ ở `/tmp/payguard-harden1-r2-review/`. Không dùng archive làm căn cứ. Coder handoff đã được đọc từ manifest: 416 passed / 2 failed / 1 xfailed trước fix verification-order, rồi 13 targeted passed; đó không phải full-suite kết quả của cây cuối.

## R2-1 — CHẶN HARDEN-1/MVP pilot: artifact model thực dùng vượt identity đã khóa

**Vị trí:** `model/__main__.py:87` lấy artifact directory từ `--data-root/--run-id`; `model/__main__.py:135-144` chỉ từ chối override simulator config; `model/__main__.py:170-189` xác minh harness/role rồi truyền nguyên artifact directory chưa được xác minh cho evaluator. `model/evaluate.py:380` nạp `conformal.joblib`/metadata từ directory đó; `model/evaluate.py:422-443` đọc truth, tính lại coverage và ghi report bằng threshold vừa nạp. `payguard/run_identity.py:158-196` xác minh source/config identity và BASE operational fingerprint nhưng không ràng buộc artifact model này.

**Phản ví dụ đã chạy:** `probe_model_artifacts.py` dựng fixture từ bản copy BASE operational/private và artifact smoke, đóng băng BASE/policy manifests bằng helper fixture với content hashes của đúng cây cuối; tạo prompt-rule/policy lock. Chỉ mock `_dirty_entries=()` tại bước tạo lock để mô phỏng cây sau commit, không mock hash, loader, role guard, evaluator hoặc model. Cả BASE và policy copy giữ nguyên; cùng policy_version/model_version, cùng test episodes `ep-0004`, `ep-0005`, cùng 326 score rows.

Copy artifact sang hai directory. Ở copy `changed`, chỉ đổi fraud conformal threshold từ `0.29236024229246327` thành `1.0` (giá trị nằm trong miền score), không đổi version hay configs. Sau đó chạy CLI công khai thật:

```bash
PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/tmp/payguard-uv-cache UV_NO_SYNC=1 GIT_OPTIONAL_LOCKS=0 PAYGUARD_BENCHMARK_STATE_DIR=/tmp/payguard-harden1-r2-review/model-state uv run python -m model evaluate --run-id changed --data-root /tmp/payguard-harden1-r2-review/model-inputs --operational-db /tmp/payguard-harden1-r2-review/model-harness/policies/rule_agent_fake/operational/observations.sqlite --private-db /tmp/payguard-harden1-r2-review/model-harness/base/private/evaluator.sqlite --episode-role test
```

**Actual:** exit 0, đọc truth test và ghi `model-inputs/changed/model/reports/evaluation.json`. Fraud coverage đổi từ **57/90 = 0.633333** thành **90/90 = 1.0**, PR-AUC và 326 score rows không đổi; legitimate coverage vẫn 236/236. Identity hashes không đổi; xác minh harness/lock vẫn PASS. `probe_model_artifacts.log` còn lưu đối chứng threshold -1 cho cả hai lớp (coverage thành 0), nhưng finding không phụ thuộc giá trị ngoài miền đó: bằng chứng cuối dùng threshold fraud hợp lệ 1.0, ở `model-threshold-valid.json`.

**Expected:** từ chối artifact/threshold không thuộc identity đã khóa trước khi mở truth, ghi role touch hay report. Đây là đường override tương đương R1-1: version giống nhau không chứng minh effective input giống nhau. Có thể thay threshold theo test mà không làm lock mất hiệu lực.

**Đề xuất:** bind model artifacts thực dùng (ít nhất conformal + metadata quyết định tập score/metric, và provenance model) vào frozen identity/manifest có content hash, xác minh trước truth/side effects. Hoặc không cho held-out model evaluation qua đường này cho tới khi binding tồn tại. Chỉ kiểm đường dẫn canonical vẫn chưa phát hiện artifact bị đổi ngay tại chỗ. Thêm functional case `--data-root/--run-id` và thay threshold giữ nguyên version; assert từ chối và không chạm truth/report/ledger.

Bằng chứng: `probe_model_artifacts.py`, `probe_model_artifacts.log`, `model-threshold-valid.json`, `model-inputs/{original,changed}/model/` trong thư mục review.

## R2-2 — KHÔNG CHẶN runtime MVP: thay mất ca hồi quy checklist-tool-error

**Vị trí:** `tests/test_evaluation_support.py:456-483`. Diff đổi tên `test_support_cli_excludes_checklist_tool_failure_from_uq_conditions` thành test runner failure; đổi `agent_run_sql.status` từ `error` sang `pass`, thêm `__runner_guard`. Các assertion count=0 còn giữ, nhưng đầu vào acceptance cũ không còn được kiểm bởi test support.

**Bằng chứng:** `git diff HEAD -- tests/test_evaluation_support.py` và so AST danh sách test trước/sau cho thấy ca cũ bị thay. `probe_runner_guard.py` đã chạy lại đúng đường CLI với `agent_run_sql.status='error'` và **không** có runner marker: exit 0, UQ (1)-(3)=0 và off=0, nên chưa thấy bug runtime. Test gate riêng không kiểm việc evaluator support nhận diện lỗi checklist.

**Đề xuất:** giữ cả hai đầu vào functional (checklist error không marker, runner error với checklist pass), ví dụ parametrize test hiện tại. Không thay ca cũ bằng ca mới. Đây là thiếu bảo toàn coverage theo yêu cầu review, không suy đoán động cơ sửa test để qua.

## Disposition R1-1 đến R1-4

| Finding cũ | Kết quả trên cây cuối |
|---|---|
| R1-1 custom standalone configs | Hai phản ví dụ gốc (đổi version; giữ version/đổi n_min=100000) đều exit 2 trước provider/DB/report/ledger. Tổng 7 probe run/measure với triage/simulator/LLM config đều từ chối; DB và state bytes không đổi. Đối chứng config mặc định có lock: triage test exit 0, 17 decisions chỉ ở test, evaluation test exit 0. Các counterexample gốc đã đóng; mục tiêu effective-input identity còn R2-1 ở model artifacts. |
| R1-2 on projection | Đóng: public support CLI same-start/prior rule → on=0; different-start/prior rule → on=0; future rule → on=1. Off=1, reason=null đúng ở cả ba fixture. |
| R1-3 provider cap | Đóng: dựng provider OpenRouter thật nhưng không gọi transport: configured cap=1.0, effective provider cap=1.0, durable shared ledger cap=10.0. Harness vẫn truyền benchmark override 10.0 (source + functional suite). |
| R1-4 model default | Đóng: probe với DB/manifests thật, không selector, exit 0 và chỉ `ep-0001` development. Không còn chọn mọi role trong harness; docs/model.md khớp. |

Giữ nguyên bất đồng lịch sử P5.0b: cả hai reviewer thấy lỗi projection; một bên xếp chặn, bên kia không chặn runtime hiện tại vì checklist seasonal chưa chạy. Review này chỉ kiểm bản sửa đã được HARDEN-1 phê duyệt, không sửa verdict cũ hay mở lại protocol.

## Checklist nghiệm thu

| Mục | Kết quả |
|---|---|
| A2 current/run-manifest/lock hashes, policy/model identity trước truth; BASE freeze, policy input=BASE | Đạt với operational/source identity. Guard stale run A/lock B, policy row mismatch và BASE fingerprint được kiểm. Effective artifact model còn R2-1. |
| A3 selectors/default CLI | Run mặc định development; chỉ episode IDs đã chọn vào runner. Standalone evaluator mặc định development, held-out qua locks. Harness evaluator mặc định mọi role như docs và kiểm locks. Model evaluate mặc định development, standalone held-out từ chối. Measure development trước, callback guard trước calibration/calls/report. |
| Effective configs run/measure/model | Đường triage/simulator/LLM override của run/measure và simulator override model bị từ chối; **chưa đạt toàn bộ** do R2-1 model artifacts. |
| B1 content resume | Git HEAD/dirty giữ dạng thông tin; hash runtime/config/prompt/sampling khác bị từ chối, lỗi nêu trường lệch. Functional harness test dùng synthetic Git, gồm commit docs/scratch resume. |
| B2 policy OpenRouter + ledger | Có K1 named policy và ledger bền chung; standalone provider $1, ledger $10, harness override $10. Không API thật. |
| B2 lock profile | Smoke root không tạo benchmark lock; pilot chuẩn hóa benchmark, profile lock độc lập. |
| C1 import boundary | Triage import verifier từ payguard.run_identity thay harness; fresh-process import test kiểm không có evaluation trong sys.modules; verifier không import evaluator. |
| C5b Git-independent tests | Harness test fixture mock Git metadata, không cần HEAD thật. |
| N1 projection | Đạt: chỉ feature/segment và rule.decision_at <= agent.decision_at; giữ incident/episode và cutoff guard. Ba probe gốc/đối chứng đạt. |
| N2/N3 support groups | Off/on null có reason theo từng nhóm; có hàng mọi nhóm truth development dù chưa match, cùng nhóm ambiguous/background theo episode. |
| N4 forced-human/code ownership | Runner tool error/invalid snapshot ghi marker có cấu trúc. Probe spoof marker từ provider bị bỏ (gate close, marker không lưu); flag code tool_error=true buộc human và marker được code tạo. Original checklist error vẫn bị support loại. |
| Gate helper/status | Support dùng helper public; status thiếu/sai bị DevelopmentSupportError. |
| Tests/protocol/docs | Không đổi configs, thresholds, split, seeds, MASTER hay verifier. Schema additions nằm ở docs/data.md; CLI docs cập nhật cùng diff. Một ca test acceptance đã bị thay đầu vào — R2-2. |
| AS-OF/truth/pending/evidence | Không thấy regression trong diff; smoke 16/16, availability/episode boundary và pending unlabeled đạt; evidence_status vẫn code tính. |

## Kiểm tra độc lập

Full suite chỉ được khởi chạy **đúng một lần**, từ frozen worktree:

```bash
PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/tmp/payguard-uv-cache UV_NO_SYNC=1 PAYGUARD_BENCHMARK_STATE_DIR=/tmp/payguard-harden1-r2-review/state uv run pytest -q --durations=15 -o cache_dir=/tmp/payguard-harden1-r2-review/pytest-cache --basetemp=/tmp/payguard-harden1-r2-review/pytest-tmp
```

UV_NO_SYNC=1 dùng môi trường đã cài, tránh uv sửa `.venv`. Chưa chạy lại full suite. Probes bổ sung chỉ nhằm các concern cụ thể đã nêu và dùng fake/no-call provider.

| Kiểm tra | Kết quả |
|---|---|
| Independent full suite | **418 passed, 1 xfailed**, exit 0, 788.95s (13:08); strict seasonal xfail đã biết. Log: `full-suite.log`. |
| Smoke generate, fresh /tmp output | Exit 0; canonical fingerprint `6defebb0a11040b32566f4a0f17e07b6e4415ad32501314f1e4b447125caf244`, integrated validation 16/16. |
| Smoke validate | Exit 0, 16 passed / 0 failed. |
| Protocol verifier bản copy /tmp | 19 examples; `cmp` output JSON với review/verification_results.json exit 0. |
| check_links | 16 files, 0 broken links. |
| git diff --check HEAD | Exit 0. |
| Source freeze trước review | HEAD/status khớp; toàn bộ 203 source hashes khớp; không file thiếu/thừa, gồm untracked run_identity.py. |
| Source freeze giữa review | Vẫn khớp; source-interim.json. |
| Source freeze sau review | **Ổn định**: 203/203 hash nguồn, HEAD và status vẫn khớp; không file thiếu/thừa. `source-final.json` trùng byte `source-interim.json`. |

Log và script ở `/tmp/payguard-harden1-r2-review/`. Không có operation bị sandbox chặn; không yêu cầu/nâng quyền.

Manifest đóng băng SHA-256: `ba5cd91741ffbd81ee023eb4782fbd16e84e154e6430afeedaf0558c90143e50`. HEAD trước/sau: `5a37211e85d8b53f5b1d3792432b8c174ba14924`. Status giữ nguyên 20 file tracked sửa và untracked `payguard/run_identity.py`; review không stale. Full suite xanh trên cây cuối không khắc phục counterexample R2-1.
