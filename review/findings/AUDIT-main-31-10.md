# Audit end-to-end main trước pilot — 31/10/2026

- **Phạm vi:** `main` lúc bắt đầu `e9e4c44`; trong lúc audit main lên `5b43333` (chỉ sửa docs: `docs/orchestration.md`, `docs/p1-implementation.md`). Mọi probe chạy trên `5b43333`.
- **Vai trò:** Reviewer/auditor. Không sửa file nào trong repo. Mọi thao tác ghi đều nằm dưới `/tmp/claude-1000/audit/`: smoke `smoke1`/`smoke2`, harness `h1`/`h2`, bản clone `clone/` để thử commit và đổi code, state dir `state1..4`.
- **Lệnh nền đã chạy:**
  - `UV_CACHE_DIR=/tmp/payguard-uv-cache uv run pytest -q` (chạy **một lần**): **393 passed, 1 xfailed** (xfail strict seasonal đã biết), 671 s.
  - `simulator generate/validate --profile smoke` vào /tmp: 16/16 PASS.
  - `python3 review/check_links.py`: 0 broken.
  - `python3 review/verify_plan_claims.py`: 19 ví dụ OK; `review/verification_results.json` không đổi.
  - Sau test, `git status` sạch và không tạo `~/.payguard-benchmark`.
- **Mức:** A = chặn pilot · B = nên sửa trước pilot · C = backlog.

---

## A. Chặn pilot

### A1. ID giao dịch do injector chèn khác định dạng với nền, nên ai đọc operational cũng tách được dòng bị chèn (lộ truth)
- **Ở đâu:**
  - `simulator/incidents.py:345` sinh ID dạng `txn_id = f"txn-{identity_rng.getrandbits(128):032x}"`.
  - Giao dịch nền dùng `simulator/transactions.py:460` dạng `txn-{episode_id}-{YYYYMMDD}-{customer_id}-d{NNNN}`.
  - Validator `simulator/validate.py:907-922` chỉ kiểm token cấm (`truth-`, `data_pipeline`, …), không kiểm định dạng ID có đồng nhất hay không, nên vẫn báo "neutral IDs are valid".
- **Phản ví dụ** (smoke `/tmp/claude-1000/audit/smoke1`, ghép với private `p1.6.incident_effect.v1:*.affected_transaction_ids`):
  - Cả 40 giao dịch dạng `txn-<32hex>` của ep-0003/ep-0005 đều nằm trong tập `affected_transaction_ids` của truth seasonal.
  - Không có giao dịch nền nào mang dạng này. Mọi dòng không bị chèn đều là `txn-ep-…-d…`.
  - Chỉ cần một câu SQL hợp lệ qua `run_sql`, ví dụ `SELECT … FROM asof.transactions WHERE txn_id NOT LIKE 'txn-' || episode_id || '-%'`, là ra đúng tập dòng injector đã chèn, tức phạm vi và thời điểm của sự cố.
  - P1.6 vòng 2 sẽ thêm injector fraud (`fraud_surge`, `new_fraud`, ca hai nguyên nhân). Nếu chép khuôn này, ID sẽ chỉ thẳng ra dòng fraud. Đây là vi phạm TRUTH CÔ LẬP và `docs/data.md` §2 ("ID … không được mã hóa … role thành phần của kịch bản").
- **Đề xuất:**
  - Sinh ID cho dòng injector cùng namespace và định dạng với nền: cấp `d{NNNN}` trong chuỗi số thứ tự của ngày/khách hàng, hoặc băm mọi ID theo một cách duy nhất.
  - Thêm check validator: tập "hình dạng" ID (regex-shape) của dòng bị chèn phải là tập con của hình dạng ID nền trong cùng episode. Thêm test có `affected_transaction_ids`.
  - Đưa yêu cầu này vào nghiệm thu P1.6 vòng 2 trước khi viết injector fraud.

### A2. Có thể chấm test trên các quyết định do code chưa khóa sinh ra: lock policy không ràng với code đã tạo decisions
- **Ở đâu:**
  - `evaluation/__main__.py:65-104`: nhánh harness chỉ gọi `assert_role_access`, mà hàm này (`payguard/identity.py:405-441`) so hash **hiện tại** với lock. Hàm không so `run-manifest.json` (hash của code đã sinh decisions) với lock, cũng không gọi `verify_harness_policy_run`.
  - `payguard/harness.py:595`: `run_triage` chạy **mọi role**, kể cả calibration/test, trước khi có lock. Decisions test được ghi sẵn vào policy copy.
- **Phản ví dụ** (clone `/tmp/claude-1000/audit/clone`, root `h1`):
  1. `payguard run --policy rule_human` ở code A sinh 21 decisions, gồm cả ep-0004/0005 (test).
  2. Sửa `triage/gate.py`, commit, chạy `payguard lock policy --policy rule_human`. Lock giờ mang code B.
  3. `python -m evaluation decisions --run h1/policies/rule_human --episode-role test` → `decision_count=17`, exit 0.
  - `run-manifest` có `code_hash 9edb8145…`, còn lock có `code_hash d35ce43d…`. Truth test được chấm trên quyết định của code chưa từng khóa.
  - `triage run` trên cùng thư mục thì bị từ chối, nghĩa là guard bất nhất giữa hai CLI.
- **Đề xuất:**
  - Evaluator (harness) bắt buộc: `lock_identity_hashes(run-manifest.hashes) == lock.identity.hashes`, `policy_version`/`model_slug` khớp, và fingerprint policy copy bằng BASE.
  - Harness không chạy triage cho episode calibration/test khi chưa có lock tương ứng (hoặc ghi identity theo từng lô decision và evaluator lọc theo identity đã khóa).
  - Thêm test hồi quy đúng chuỗi 1→3 ở trên.

### A3. Ngoài harness không có guard role: `evaluation decisions` bỏ qua `--episode-role` và đọc truth test không cần lock; `model evaluate` in truth của mọi role
- **Ở đâu:**
  - `evaluation/__main__.py:58` đặt `selected_episode_ids = None` và chỉ gán lại trong `if harness_root is not None` (dòng 65). Vì vậy `--episode-role` bị **lờ im lặng** với run P4.
  - `model/__main__.py:88-99` cùng `model/evaluate.py:369-411` chấm mọi episode bằng truth giao dịch, không có lọc role hay lock.
  - Docs `evaluation.md` §9 dòng 105 nói chung cho cả lệnh: "Có thể lặp `--episode-role` để giới hạn role".
- **Phản ví dụ:**
  - Copy operational + private ra `/tmp/claude-1000/audit/p4run` (không có harness manifest), chạy `PAYGUARD_BENCHMARK_STATE_DIR=…/state2 python -m evaluation decisions --run …/p4run --episode-role development`. Report có `episode_ids ['ep-0001','ep-0002','ep-0003','ep-0004','ep-0005']` và truth của 2 episode test. Không ghi role ledger: `state2` không được tạo.
  - `python -m model evaluate … --private-db h2/base/private/evaluator.sqlite` in PR-AUC/coverage cho ep-0004 = 0,772, ep-0005 = 0,842 (test). Không ledger, không lock.
- **Vì sao chặn pilot:**
  - Harness chỉ hỗ trợ smoke (`payguard/harness.py:43` `PROFILE = "smoke"`; xem B2). Đường hiện có để chạy pilot vì vậy là CLI P4, hoàn toàn không có guard KHÔNG TUNE TRÊN TEST.
  - Alpha conformal là "cấu hình thực nghiệm" (`docs/model.md` §3), tức có thể bị chỉnh nhìn vào `model evaluate` trên test.
- **Đề xuất:**
  - Run không thuộc harness có episode calibration/test: hoặc từ chối, hoặc bắt `assert_role_access` với state dir. Tôn trọng `--episode-role` ở mọi nhánh.
  - `model evaluate` mặc định chỉ development, role khác phải qua lock và ghi ledger.
  - Sửa câu trong `evaluation.md` §9 cho khớp.

---

## B. Nên sửa trước pilot

### B1. Danh tính harness gắn Git HEAD + cờ dirty: một commit chỉ sửa docs hoặc một file untracked bất kỳ chặn resume, chặn thêm policy thứ hai trên cùng BASE, chặn triage standalone
- **Ở đâu:**
  - `payguard/identity.py:197-198`: `git_head`/`git_dirty` nằm trong `current_hashes`.
  - So sánh toàn bộ ở `payguard/harness.py:114`, `:138`, `:459`.
  - Thông báo lỗi "current code/config/prompt/sampling hashes do not match" gây hiểu nhầm khi chỉ HEAD đổi.
- **Phản ví dụ** (clone):
  - Sau `payguard run --policy rule_human --out h1` và `lock prompt-rule`, commit `docs/overview.md`.
  - `payguard run --policy rule_agent_fake --out h1` → `harness resume refused`.
  - `triage run --run h1/policies/rule_human` → refused.
  - `evaluation decisions --episode-role calibration` vẫn chạy (lock bỏ HEAD).
  - Chỉ `touch scratch.txt` (untracked) cũng làm `triage run` bị từ chối; xóa file là chạy lại được.
- **Vì sao quan trọng:** orchestrator commit docs liên tục (chính trong lúc audit có commit `5b43333`). So sánh rule_human với hybrid đòi cùng BASE. Với provider thật, mỗi lần mở root mới phải chạy lại triage và tốn lại ngân sách LLM.
- **Đề xuất:**
  - Resume harness/BASE/policy dùng `lock_identity_hashes` (chỉ nội dung runtime); giữ `git_head` làm thông tin.
  - Hoặc chỉ tính dirty trên các đường dẫn runtime/config/prompt.
  - Sửa thông báo lỗi. Cập nhật `docs/data.md` §9 trong cùng thay đổi.

### B2. Harness chưa chạy được ở cỡ pilot với LLM thật; lock profile `benchmark` chưa nối với gì nhưng vẫn tạo được từ root smoke
- **Ở đâu:**
  - `payguard/harness.py:43` `PROFILE="smoke"`; `payguard/__main__.py` `run --profile` chỉ nhận `smoke`.
  - `payguard/identity.py:31-34` `POLICY_PROVIDERS` chỉ có `rule_human` (unconfigured) và `rule_agent_fake`, không có policy OpenRouter.
  - `triage/__main__.py:76-81`: chạy ngoài harness thì provider không có `DurableCostLedger`, nên cap $1 trong `configs/llm.yaml` chỉ là cap **trong bộ nhớ, mỗi tiến trình** (`triage/llm.py:271`), không cộng dồn.
  - `create_lock` (`payguard/identity.py:340-380`) không kiểm profile của root.
- **Hệ quả:**
  - P5.0d ("một lượt thật qua sổ chi phí bền P5.1, tính vào trần 10 USD") không làm được trên main hiện tại. Lượt thật nào cũng nằm ngoài ledger 10 USD.
  - Tên profile lệch: simulator dùng `smoke`/`pilot`, lock dùng `smoke`/`benchmark`.
  - Phản ví dụ: `payguard lock prompt-rule --profile benchmark --out h1` (h1 là root smoke) chạy thành công và ghi `state1/locks/benchmark/prompt-rule.json` bất biến với hash của code hiện tại. Lỡ tay một lần là khóa vĩnh viễn calibration của benchmark.
- **Đề xuất:**
  - Thêm policy harness cho OpenRouter (rule+agent K = 1/K = 3) dùng ledger chung.
  - Chuẩn hóa ánh xạ profile pilot ↔ lock.
  - `create_lock` từ chối khi profile lock khác `harness-manifest.profile`.
  - CLI ngoài harness với provider thật nên dùng ledger bền hoặc từ chối.

### B3. Cơ cấu pilot lệch giữa docs và code; phép chia mới chưa qua kiểm chứng protocol
- **Ở đâu:**
  - `configs/episodes.yaml:187-188` `multi_cause_episode_count: 4` với provenance "docs/data.md, section 4".
  - `simulator/config.py:512` cứng `!= 4 → lỗi`.
  - `docs/data.md:142` (chốt 31/10) yêu cầu 10 ca hai nguyên nhân (2/4/4) + 5 ca fraud trùng lịch (1/2/2).
  - Chưa có trường config cho nhóm "fraud trùng lịch". `review/verify_plan_claims.py` không đổi (lần sửa gần nhất `997817f`), trái quy tắc AGENTS "Sửa protocol → cập nhật verify_plan_claims".
- **Phản ví dụ số học:**
  - Development có 8 episode injected, trừ 2 ca hai nguyên nhân và 1 ca fraud trùng lịch, còn **5 suất** cho 7 kịch bản một nguyên nhân (`docs/data.md` mục Kịch bản).
  - `data_pipeline` và `fraud_surge` mỗi nhóm cần ≥ 1, nên còn 3 suất cho 5 kịch bản (seasonal đơn, legit_business_spike, merchant ít giao dịch, new_fraud, concept drift).
  - Vậy **ít nhất 2 kịch bản không có episode development nào** để viết rule/ngưỡng (`evaluation.md` §1).
  - Seasonal một nguyên nhân (cần cho ngưỡng volume đang hoãn và runbook `seasonal_calendar`) có thể rơi vào nhóm này.
- **Đề xuất:**
  - Trước P1.7 vòng 2, orchestrator/người dùng chốt bảng phân bổ 60 episode theo kịch bản × role.
  - Sửa `episodes.yaml` + `config.py` + validator cùng lúc. Thêm ví dụ số học vào `verify_plan_claims.py`.

### B4. Seed episode chỉ phụ thuộc `(root_seed, episode_id)`: pilot sẽ dùng lại seed smoke đã bị soi
- **Ở đâu:** `simulator/episodes.py:55-59` `derive_seed(root_seed, "p1.7.episode.v1", episode_id)`, không có khóa profile; `configs/episodes.yaml` có một `seed: 20261026` chung.
- **Phản ví dụ:**
  - `derive_episode_seed(20261026,'ep-0002'..'ep-0005')` = `[1956819742723152685, 8517837957079428009, 7577381676897689475, 5634288665361320469]`, trùng y `episode_manifest` của smoke.
  - Dữ liệu calibration/test của các seed này đã bị xem khi thiết kế, ví dụ `review/findings/P3.7-r1.md:54,68,95` phân tích PSI/volume của ep-0002/0003/0004 để chọn bin/ngưỡng.
  - `evaluation.md` §1 và `docs/p1-implementation.md:186` yêu cầu seed calibration/test **mới**. Nếu pilot gán ep-0002…ep-0005 vào calibration/test thì held-out không còn held-out.
- **Đề xuất:**
  - Thêm profile (hoặc root seed riêng cho pilot) vào phép dẫn xuất.
  - Validator pilot kiểm seed calibration/test không giao với seed smoke.
  - Ghi vào spec P1.7 vòng 2.

### B5. Baseline rule + human ghi quyết định `source='agent'`
- **Ở đâu:** `triage/runner.py:94-115`. Mọi incident không qua rule đều gọi provider; `UnconfiguredAgent` trả về decision `source='agent'`, `model_slug=NULL`.
- **Phản ví dụ:** `h1/policies/rule_human` có `agent|NULL|human_review|insufficient|19` và `rule|NULL|data_ticket|2`.
- **Hệ quả:**
  - Các bảng "theo nguồn rule/agent" (`evaluation.md` §4, P5.0c T5) sẽ tính 19 lần chuyển người của baseline vào nhánh agent.
  - Rule+human và rule+agent không tách được theo `source`.
- **Đề xuất:** trước P5.0c, chốt mapping (ví dụ `source='agent' AND model_slug IS NULL` ⇒ "human handoff"), hoặc thêm giá trị nguồn riêng, và sửa `docs/data.md` §8.

---

## C. Backlog

1. **Triage import evaluator gián tiếp, trái `docs/data.md` §8 ("module triage không import evaluator").**
   - Chuỗi import: `triage/__main__.py:26` → `payguard/harness.py:14` → `evaluation.decisions`.
   - Lệnh kiểm: `uv run python -c "import sys, triage.__main__; …"` thấy `evaluation`, `evaluation.decisions`, `evaluation.matcher`, `evaluation.recall` trong `sys.modules`.
   - Đề xuất: chuyển `verify_harness_policy_run` và `BENCHMARK_COST_CAP_USD` sang module không import evaluation; thêm test ranh giới import.
2. **Runner gộp `_agent_checklist_results` của provider đè lên checklist do code tính mà không giới hạn tên** (`triage/runner.py:106-113`).
   - `OneStepAgent` hiện chỉ thêm `agent_run_sql`/`agent_proposal`, nên chưa khai thác được.
   - Provider P4c có thể đè `unit_or_null_change`/`seasonal_calendar`, từ đó đổi `evidence_status`.
   - Đề xuất: từ chối mọi key trùng checklist của code, hoặc bắt tiền tố `agent_`.
3. **Report ghi `metric_window_days: 7` (một số nguyên) trong khi matcher dùng cửa sổ theo metric** (volume = 1).
   - Ở đâu: `evaluation/decisions.py:386`, `docs/data.md:284,336`.
   - Report không cho biết cửa sổ thực đã áp. Đề xuất: thêm `metric_windows` theo metric.
4. **Chép hợp đồng giữa các docs (trái quy tắc một chỗ):**
   - Định nghĩa metric volume: `docs/data.md:232` ↔ `docs/monitoring.md:12`.
   - Luật ngưỡng alert `abs(value − expected) > threshold`: `docs/data.md:250` ↔ `docs/monitoring.md:13`.
   - Nội dung và tính bất biến của lock marker: `docs/data.md:372,376` ↔ `docs/p5-implementation.md:87`.
   - Đề xuất: giữ một bản gốc, chỗ còn lại link tới.
5. **Chất lượng test:**
   - (a) `tests/test_monitoring_metrics.py:761-776` *bắt buộc* phải có volume alert trong episode seasonal, mà thứ đếm được là alert nền T0 (false positive). Test khóa hành vi sai: sửa lệch pha T0 sẽ làm test đỏ. Nặng hơn mục F2 đã biết (F2 chỉ nói về thông điệp assert). Nên bỏ assertion này hoặc đảo chiều.
   - (b) `tests/test_payguard_harness.py:57-85` chạy hai `run_benchmark` cách nhau khoảng 35 s và đọc Git HEAD thật. Orchestrator commit giữa chừng là test đỏ (commit `5b43333` đã rơi vào giây thứ 3 sau khi suite bắt đầu). Nên monkeypatch `_git`/`current_hashes` như các test lock.
6. **Bộ đếm recheck và trạng thái incident không lọc theo `policy_version`/`model_slug`** (`triage/runner.py:146-170`, `monitoring/incidents.py:82-104`).
   - Run dir P4 dùng lại qua nhiều policy version sẽ trộn recheck và incident con của version cũ. Harness đã cô lập bằng bản copy riêng.
   - Đề xuất: lọc theo policy hiện tại, hoặc từ chối run dir có nhiều `policy_version`.

---

## Đã kiểm, OK

- **TRUTH qua `run_sql`:** 12 probe dùng `FakeLLMClient` (`/tmp/claude-1000/audit/probes/run_sql_probe.py`) đều bị chặn với `sql_execution_error`:
  - `main.transactions`, bảng không ghi schema;
  - `sqlite_master`, `temp.sqlite_master`, `asof.sqlite_master`;
  - `pragma_table_info`, `pragma_database_list`;
  - `ATTACH` sau `WITH …;`;
  - `asof.episodes`.

  Ngược lại, `asof.*` chỉ có `base` + episode đang xét và `available_at ≤ as_of`. `run_sql` cũng từ chối DB chứa bảng private (`triage/tools.py:122`).
- **Đường mở store private:** chỉ evaluator (`evaluation/*`), validator/injector/report của simulator, và `model evaluate` (xem A3). Model score/train, monitoring, triage, gate không mở private và không đọc `configs/episodes.yaml`. Prompt agent chỉ chứa trường incident trung tính + checklist + kết quả SQL (`triage/agent.py:39-50`). Report public `reports/smoke.json` không chứa template/truth. Policy dir harness không chứa file private.
- **AS-OF:**
  - Mọi feature/metric/runbook đọc qua `read_as_of`. Các SELECT trực tiếp còn lại chỉ là kiểm idempotent/dedup/đếm (`model/evaluate.py:161`, `monitoring/store.py:78`, `triage/runner.py:132,187`).
  - Probe dữ liệu đến muộn (`/tmp/claude-1000/audit/probes/late_probe.py`): chèn giao dịch ×1000 và nhãn fraud có `available_at` 30/03. Checklist `unit_or_null_change`, kết quả `run_sql` và hash snapshot đều không đổi.
- **PENDING ≠ NHÃN ÂM:**
  - `model/data.py:203-211`: `pending → None`, bị loại.
  - `matured_negative` chỉ khi tuổi ≥ H.
  - Fit/calibration chỉ lấy cohort tuổi ≥ H tại T0.
  - Validator `pending_is_not_negative` PASS (1085 snapshot).
  - Monitoring không dùng nhãn.
- **`evidence_status`:**
  - `_normalize_proposal` chỉ nhận đúng bộ key `{cause, action, reason[, runbook]}`; thừa key `evidence_status` là rơi về fallback `human_review`.
  - Runner `pop("evidence_status")`; gate tính lại từ checklist (`triage/gate.py:250-300`).
- **Giá trị khóa khớp docs ↔ config/code:**
  - PSI 0,25, 2 bin, epsilon 1; volume 4,25 (tính lại: max residual ep-0001 = 4,25 ngày 01/02);
  - `n_min` 2; recheck ≤ 2 và ≥ 24 h (`triage/gate.py:707-720`); tau ∈ {NULL, 2/3, 1} (`triage/gate.py:80-90`);
  - D = 7 (`simulator/incidents.py:19`, `simulator/validate.py:45`); cap 1 USD (`llm.yaml`) và 10 USD (`benchmark.yaml`);
  - 12/24/24 và clean 4/8/8 được enforce. Ngoại lệ: cơ cấu injected, xem B3.
- **Schema:** 14 bảng trong `docs/data.md` so với `PRAGMA table_info` của smoke mới sinh (`/tmp/claude-1000/audit/schema_cmp.py`) khớp tên, thứ tự, kiểu và nullability. `model_scores`/`decisions` khớp bảng §6/§8; có các index as-of.
- **Tái lập:**
  - Hai lần `generate --profile smoke` cho operational DB giống từng byte. Private chỉ khác `checked_at`/`runtime.*`; seed/fingerprint manifest trùng.
  - Hai harness root cho cùng BASE fingerprint `12a556af…` (806 score, 21 alert).
  - Lock không đổi sau commit chỉ sửa docs (evaluator calibration vẫn chạy).
- **Bộ test:** xfail duy nhất là strict seasonal đã biết, không có skip. Test harness ép state dir vào `tmp_path` (autouse). Không test nào ghi vào repo hay `~/.payguard-benchmark`. `.gitignore` phủ `data/`, `.venv/`, `.env`.
- **Đã biết, không nhắc lại:** seasonal-volume-match hoãn; vòng 1 chưa có checklist `seasonal_calendar`; agent close tắt; alert volume nền tại T0 ở ep-0003/ep-0004 (đã ghi ở `docs/p3-implementation.md:106`).
