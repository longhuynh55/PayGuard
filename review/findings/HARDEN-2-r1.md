# Review HARDEN-2 — vòng 1

- **Reviewer:** Claude Opus 5.5 (sub-agent mới, do orchestrator gọi)
- **Nhánh/commit:** `slice/harden-2` @ `c74bc80` (task `f574a99` + merge main `dd6ab22`); diff task `git diff 1c39805 f574a99`
- **Worktree:** `/home/lieu_hoai_phuc/.herdr/worktrees/Aiagent/slice-harden-2`. `git status --short` rỗng cả trước và sau review. `review/verification_results.json` không đổi (`sha256sum -c` OK).
- **Scratch:** toàn bộ ở `/tmp/claude-1000/harden2-r1/`, gồm state dir tạm `state/`, root e2e `e2e/`, các probe `probe_*.py` và log `*.log`. Chỉ dùng fake provider, không gọi API thật.
- **Full suite:** không chạy lại. Dùng kết quả orchestrator đã chạy trên cây này: 423 passed, 1 xfailed, 756 s.

## Kết quả chính

| Mục | Kết quả |
|---|---|
| R2-1: đổi threshold, giữ version | **Đóng.** Harness e2e thật, sau lock: exit 2 ở cả 4 tập role (test, calibration, dev+test, dev). Không mở `evaluator.sqlite`, không ghi report, state dir giữ nguyên byte. |
| Biến thể metadata / predictor / calibrator / thiếu file / sửa tại chỗ | Đều exit 2 và lỗi nêu đúng tên file lệch. Không mở truth, không ghi report, ledger không đổi. |
| Copy byte giống hệt sang `--data-root` khác | Exit 0, fraud coverage 57/90, giống bản canonical. Đúng như kỳ vọng. |
| Artifact đúng sau lock (dev/cal/test/mặc định) | Exit 0. Report ghi tại `artifacts/payguard-p5/model/reports/evaluation.json`, khớp docs. Ledger ghi đúng role. |
| Resume `payguard run` khi artifact BASE bị đổi | Exit 2 với `frozen BASE model artifact content hashes differ: conformal.joblib`. Ledger không đổi. |
| NB-1: relabel `episodes.role` ngoài harness | **Đóng.** Exit 2 trước truth (spy `_truth_for_episode` không ghi nhận lần gọi nào). Gọi thẳng API `evaluate_model_scores` cũng bị chặn. |
| NB-2: nhánh chết | **Đóng.** Đã xử lý có chủ đích: nhánh "outside harness limited to development" giờ chạy tới được và có test. |
| Giới hạn check artifact chỉ ở `model evaluate` | Không làm yếu HARDEN-1. Ngoài `payguard/harness.py` và `model evaluate`, không module nào trong triage/evaluation/monitoring nạp artifact model. |
| Hồi quy HARDEN-1 (A2/A3/B1/B2/C1) | Không thấy: 15 test có mục tiêu đều pass, probe thiếu lock exit 2 trước truth, ranh giới import vẫn đạt. |
| Docs ↔ code | Khớp tên trường, danh sách 4 file và thứ tự kiểm. Có 2 lệch nhỏ (NB-H2-3). |
| Smoke / verify / links | Smoke 16 passed / 0 failed; `verify_plan_claims` 19 ví dụ; `check_links` 16 file, 0 link hỏng. |

## Kiểm tra chi tiết

### (1) Phản ví dụ R2-1 trên harness thật
Dựng root thật, không dùng fixture:
```
PAYGUARD_BENCHMARK_STATE_DIR=/tmp/claude-1000/harden2-r1/state uv run python -m payguard run --profile smoke --policy rule_agent_fake --out /tmp/claude-1000/harden2-r1/e2e
uv run python -m payguard lock prompt-rule --profile smoke --out .../e2e      # rc=0
uv run python -m payguard lock policy --profile smoke --policy rule_agent_fake --out .../e2e   # rc=0
```
- `base-manifest.json.model_artifacts` có `schema_version: 1`, `files` gồm 4 file, và `content_sha256 = 9e9f45…`.
- `run-manifest.json.model_artifacts` bằng đúng bản của BASE.

Probe `probe_artifacts.py` (log `probe_artifacts.log`) gọi `model.__main__.main`, spy `sqlite3.connect` và chụp hash toàn bộ state dir trước và sau mỗi ca. Threshold fraud gốc là `0.29236024229246327`; bản `thr` đổi thành `1.0`:
```
{"case":"thr","roles":["test"],"rc":2,"err":"ERROR: RunIdentityError: frozen model artifact content hashes differ: conformal.joblib","private_opened":false,"report":false,"state_unchanged":true}
```
Kết quả các biến thể:
- **`thr`, `meta`, `pred`, `calib`:** đều exit 2 và lỗi nêu đúng tên file lệch, ở cả 4 tập role kể cả development.
- **`missing`:** báo `required model artifact is missing or unreadable`.
- **`canonical-inplace-thr`** (sửa ngay file canonical): exit 2, lỗi `conformal.joblib`.
- **Mọi ca bị từ chối:** `private_opened=false`, không có report, state dir giữ nguyên byte.

### (2) Artifact đúng chạy được sau lock
| Ca | Kết quả |
|---|---|
| `identical-copy-other-dataroot`, test | rc 0, `ep-0004`, `ep-0005`, fraud 57/90 |
| `canonical-dev` | rc 0, `ep-0001` |
| `canonical-cal` | rc 0, `ep-0002`, `ep-0003`, fraud 64/90 |
| `canonical-test` | rc 0, fraud 57/90 |
| `canonical-default` (không truyền role) | rc 0, chỉ `ep-0001` |

Ledger có các entry `model.evaluate.cli` cho development, calibration và test.

Tôi cũng sửa `conformal.joblib` canonical rồi chạy `payguard run --policy rule_human` trên cùng root: rc 2 với `frozen BASE model artifact content hashes differ: conformal.joblib`. Ledger vẫn `OK` (sha256), sau đó tôi khôi phục file.

### (3) NB-1 — relabel role
Probe `probe_relabel.py` (log `probe_relabel.log`) copy DB operational/private ra ngoài harness, drop trigger rồi UPDATE:
- **`ep-0004` từ test thành development:** chạy mặc định và `--episode-role development` đều rc 2 với `selected operational episode roles do not match private episode_manifest: [('ep-0004', 'development', 'test')]`, `truth_read_for=[]`.
- **Đổi chéo dev ↔ test, và relabel calibration thành dev:** cùng rc 2, không đọc truth.
- **`--episode-role test` ngoài harness:** rc 2 với "outside a P5.1 harness is limited to development episodes".
- **Gọi API `evaluate_model_scores(..., episode_ids=None)` trên DB đã relabel:** `ValueError` trước truth.
- **Đối chứng copy sạch:** rc 0, chỉ đọc truth của `ep-0001`.
- **Relabel trong policy copy của harness:** rc 2 với `input fingerprint differs from frozen BASE`. Sau khi khôi phục, dev rc 0.

### (4) NB-2
Khối từ chối tạm thời đã bị xóa. Điều kiện `any(role != "development")` được thay bằng `held_out_roles` (tương đương), ở `model/__main__.py:140`. Lỗi "outside a P5.1 harness is limited to development episodes" chuyển vào `elif held_out_roles` (`model/__main__.py:175-178`), giờ chạy tới được và có assert trong `test_smoke_cli_scores_episode_asof_and_evaluates_private_truth`. Với mọi role, harness luôn qua `verify_harness_model_artifacts` → `verify_harness_policy_run` → private role check → `assert_role_access`. Không thấy đường role nào né được các check này.

### (5) Thứ tự kiểm
Trình tự trong `model/__main__.py:107-206`:
1. Đọc role từ operational DB ở chế độ read-only.
2. Kiểm đường dẫn harness, private store và simulator config.
3. `verify_harness_model_artifacts` (`:170`).
4. `verify_harness_policy_run`: lock, hash, fingerprint (`:174`).
5. Private `episode_manifest` (`:182-183`), chỉ SELECT `episode_id, role`.
6. `assert_role_access`: ghi ledger (`:186`).
7. `evaluate_model_scores`: assert hash lần nữa, nạp artifact, kiểm role lần nữa, đọc truth, ghi report.

Mọi check đều nằm trước side effect. Probe thiếu lock (`probe_nolock.py`) cho kết quả:
- **Thiếu policy lock, role test:** rc 2, không mở private, ledger không đổi.
- **Thiếu prompt-rule lock, role calibration:** rc 2, không mở private, ledger không đổi.

Hai điểm TOCTOU nhỏ:
- Hash xong thì `joblib.load` đọc file lại lần nữa (`model/evaluate.py:415-422`), xem NB-H2-2.
- Thư mục `reports/` không nằm trong phạm vi hash. Điều này hợp lý vì report là output.

### (6) Docs ↔ code
- `docs/data.md:391-392,399`: tên trường `model_artifacts`, `{schema_version, files, content_sha256}` và 4 file khớp `model/artifacts.py:11-16,29-41`. Resume so `model_artifacts` khớp `payguard/harness.py:354-358,455`.
- `docs/model.md:52-55` và `docs/evaluation.md:107` khớp hành vi: harness cần lock, ngoài harness chỉ dev, role được đối chiếu với `episode_manifest` trước truth, report đặt ở `model/reports/evaluation.json`.
- Lệch nhỏ: xem NB-H2-3.

### (7) Hồi quy HARDEN-1
```
uv run pytest -q -p no:cacheprovider --basetemp=/tmp/claude-1000/harden2-r1/pt-tmp \
 tests/test_model_scores.py::test_model_evaluate_refuses_changed_harness_artifacts_before_truth_report_or_role_ledger \
 tests/test_model_scores.py::test_model_evaluate_standalone_rejects_episode_role_relabel_before_truth \
 tests/test_model_scores.py::test_harness_model_evaluate_without_selector_uses_development_only \
 tests/test_payguard_harness.py::{test_heldout_harness_triage_waits_for_lock_and_stale_run_cannot_be_scored,test_lock_profile_must_match_root_and_pilot_maps_to_benchmark,test_triage_cli_run_and_measure_refuse_real_identity_mutations,test_lock_identity_ignores_git_metadata_but_rejects_runtime_hash_change,test_standalone_openrouter_triage_uses_durable_benchmark_ledger} \
 tests/test_evaluation_decisions.py::{test_triage_runtime_modules_do_not_import_evaluation,test_p4_cli_honors_episode_role_and_defaults_to_development} \
 tests/test_triage_runner.py::test_triage_runtime_avoids_evaluation_and_episode_loader_imports \
 tests/test_triage_measure.py::test_measure_checks_calibration_lock_before_reading_calibration_candidates
→ 15 passed in 66.25s
```
- **C1 (ranh giới import):** `import triage.__main__` giờ nạp thêm `model.artifacts`, `model.data` và `model.features` qua `payguard.run_identity`. Đây đều là module operational/stdlib, không có `evaluation` hay truth, nên C1 vẫn đạt.
- **Giới hạn check artifact:** `verify_harness_policy_run` (`payguard/run_identity.py:530-537`) chỉ so artifact khi một trong hai manifest có `model_artifacts`. Trên root thật, cả hai manifest luôn có trường này, và harness từ chối BASE không có (`payguard/harness.py:386-388`). Xóa trường khỏi cả hai manifest sẽ làm `run_fingerprint` lệch. Grep cho thấy triage/evaluation/monitoring không nạp `joblib`/conformal/metadata, nên các đường decisions/support không dùng artifact và không bị né.

### (8) Smoke và protocol
```
uv run python -m simulator generate --profile smoke --out /tmp/claude-1000/harden2-r1/smoke   # rc 0
uv run python -m simulator validate --run /tmp/claude-1000/harden2-r1/smoke                   # 16 passed, 0 failed
python3 review/verify_plan_claims.py   # Verified 19 illustrative examples; verification_results.json OK (không đổi)
python3 review/check_links.py          # Checked 16 files, 0 broken links.
```

## Findings

### NB-H2-1 — không chặn: lock không ràng `model_artifacts`; sửa đồng bộ manifest thì vẫn qua
- **Vị trí:** `payguard/run_identity.py:145-177`, `:526-543`. Lock marker chỉ chứa hash code/config/prompt/sampling, theo `docs/data.md:393`.
- **Phản ví dụ:** ca `forged-manifests-with-runfp` trong `probe_artifacts.py` sửa `conformal` (threshold 1.0), rồi ghi `model_artifacts` mới vào cả `base-manifest.json` lẫn `run-manifest.json` và tính lại `run_fingerprint` bằng `payguard.identity.run_fingerprint`. Kết quả: rc 0, fraud coverage 90/90. Nếu chỉ sửa manifest mà không tính lại fingerprint thì bị chặn: `policy resume refused: run-manifest fingerprint is inconsistent`.
- **Mức độ:** muốn làm vậy phải cố ý giả mạo ba chỗ trong JSON. BASE `input_fingerprint` của HARDEN-1 cũng tin manifest theo đúng cách này. Spec chỉ yêu cầu "frozen identity/manifest", và tiêu chí nghiệm thu (đổi threshold giữ version → từ chối) đã đạt.
- **Đề xuất:** nếu người dùng muốn chặn cả trường hợp này, ghi `model_artifacts` vào policy lock lúc `lock policy` (BASE đã freeze trước lock) và so khi chấm test. Đây là thay đổi hợp đồng nên để người dùng chốt.

### NB-H2-2 — không chặn: TOCTOU giữa lúc hash và lúc nạp artifact
- **Vị trí:** `model/evaluate.py:415-422` gọi `assert_model_artifact_identity` rồi mới `_read_model_artifacts`, nên mỗi file bị đọc 2 lần.
- **Mức độ:** chỉ là cửa sổ race lý thuyết. Harness không có process lock và docs đã yêu cầu chạy tuần tự.
- **Đề xuất (tùy chọn):** đọc bytes một lần, hash rồi nạp từ chính bytes đó.

### NB-H2-3 — không chặn: lệch docs nhỏ
- `docs/data.md:399`: câu "Trước khi CLI `model evaluate` … artifact directory được chọn phải khớp identity trong cả BASE và policy run manifest" thiếu chữ "trong harness". Ngoài harness (chỉ dev) không có check này. Đề xuất thêm "Trong P5.1 harness,".
- `docs/p5-implementation.md:62` vẫn ghi "`model evaluate` tạm chặn calibration/test tới HARDEN-2". Orchestrator nên cập nhật sau merge, cùng với trạng thái HARDEN-2.
- `payguard/run_identity.py:167` hard-code `"payguard-p5"` thay vì dùng `MODEL_RUN_ID`. Đây là nhánh mặc định, CLI không dùng tới.
- `tests/test_payguard_harness.py:661-665` monkeypatch `run_identity_module.verify_harness_model_artifacts`, nhưng `verify_harness_policy_run` không gọi hàm này nên patch không có tác dụng.

### NB-H2-4 — không chặn, ghi nhận: report model chấm held-out nằm ngoài `base/private/`
- **Vị trí:** `model/evaluate.py:493-497` ghi `<root>/artifacts/payguard-p5/model/reports/evaluation.json`, và giờ file này có thể chứa metric test/calibration lấy từ truth. Report decisions thì nằm dưới `base/private/reports/`.
- **Mức độ:** hành vi đã có từ trước với development, và docs (`docs/model.md:55`) ghi đúng vị trí này. Tool agent chỉ đọc snapshot operational qua `run_sql`; grep triage không có đọc `artifacts`/`reports`. Vì vậy chưa vi phạm TRUTH CÔ LẬP.
- **Đề xuất (tùy chọn, hỏi người dùng):** chuyển report held-out vào `base/private/reports/model/` cho đồng nhất với P5.1.

Không có finding chặn MVP. Tiêu chí nghiệm thu HARDEN-2 đã đạt: đổi threshold giữ nguyên version bị từ chối mà không chạm truth/report/ledger, artifact đúng chạy được sau lock. NB-1 và NB-2 đã đóng.

VERDICT: PASS
