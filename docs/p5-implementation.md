# P5 — Sub-plan chuẩn bị đánh giá

> Trạng thái: **người dùng duyệt 30/10/2026** sau phản biện kiến trúc (orchestrator + supervisor). Hợp đồng chấm điểm: [evaluation.md](evaluation.md); schema: [data.md](data.md); triage: [triage.md](triage.md). File này sở hữu thứ tự và nghiệm thu các task chuẩn bị P5; không chép hợp đồng.
> Điều phối: [orchestration.md](orchestration.md). Ngân sách và số episode đã khóa: [MASTER_PLAN.md](../MASTER_PLAN.md) mục 5.

## Vì sao làm trước P1.4

Phản biện 30/10 chỉ ra ba rủi ro chặn câu hỏi nghiên cứu: (1) chưa chứng minh có ca mà điều kiện score UQ đổi được quyết định ([evaluation.md](evaluation.md) mục 6 thừa nhận A ≡ B có thể xảy ra); (2) matcher chưa có hiệu ứng trễ cửa sổ và ca mơ hồ nên đếm support benign sẽ sai; (3) không có harness chạy nhiều policy trên cùng input, và trần 10 USD chưa được thực thi qua nhiều tiến trình. Thứ tự 4 đợt P1 vòng 2 giữ nguyên; các task dưới đây chen trước P1.4.

## Task

| Task | Ai | Phụ thuộc | Nội dung |
|---|---|---|---|
| P5.0a Bảng quyết định UQ ✅ 31/10 (mục P5.0a dưới) | Orchestrator (tài liệu) | — | Liệt kê theo nhóm sự cố trong [data.md](data.md) mục Kịch bản: ca nào tới agent, quyền đóng nào áp dụng, ca nào điều kiện `close_score >= tau_agent` có thể đổi close/routing. Kết luận chấp nhận cả "không thêm giá trị" |
| P5.2 Matcher hoàn chỉnh | Codex | — | Như mục P5.2 dưới |
| P3.7 Tính hợp lệ monitoring ([sub-plan P3](p3-implementation.md)) | Codex | P5.2 | Metric volume theo ngày + sửa alert nền `amount_avg_7d` (người dùng duyệt 31/10) |
| P5.0b Bảng support development | Codex | P5.2, P3.7 | Như mục P5.0b dưới |
| P5.1 Harness + sổ chi phí | Codex | P4.1 vòng 2 merge | Như mục P5.1 dưới |
| HARDEN-1 Guard role/lock + identity harness (người dùng duyệt 31/10, [audit](../review/findings/AUDIT-main-31-10.md) A2, A3, B1, B2, C1, C5b) | Codex | P5.0b | Như mục HARDEN-1 dưới |
| P5.0c Report shell (người dùng duyệt 31/10, đề xuất brainstorm 2) | Codex | P5.0b, HARDEN-1; từ 31/10 sau P4 vòng 2, trước khóa prompt/rule (người dùng đổi thứ tự) | Như mục P5.0c dưới |
| HARNESS-PILOT (người dùng duyệt 02/11) | Claude Haiku 5.5 max | P1.8 vòng 2 | Như mục HARNESS-PILOT dưới; điều kiện cho P4.6 ca thứ ba, P5.0b chạy lại trên pilot, P5.0d |
| P5.0d Khả thi chi phí pilot (người dùng duyệt 31/10, đề xuất brainstorm 3) | Codex | P5.0b, HARDEN-1; từ 31/10 sau pilot 60 P1 vòng 2 và P5.0b chạy lại trên development pilot (người dùng đổi thứ tự) | Như mục P5.0d dưới |

### P5.2 — Matcher hoàn chỉnh trước calibration
- **Mục tiêu:** matcher đúng [evaluation.md](evaluation.md) mục 2: thời gian gồm hiệu ứng trễ của cửa sổ metric, segment, feature; trạng thái `matched` / `ambiguous_match` / `unmatched`.
- **Nghiệm thu:** fixture phản ví dụ cho (a) incident mở muộn trong cửa sổ trễ vẫn `matched`; (b) hai truth cùng ứng viên → `ambiguous_match`, không chấm accuracy (ngoại lệ đã chốt 31/10: cùng `parent_scenario` khác NULL → `matched` nhiều truth, [evaluation.md](evaluation.md) mục 2); (c) ca nhiều nguyên nhân; (d) unmatched không thành benign. Matcher khóa trên development; không đọc calibration/test để chỉnh. Hành vi trên smoke hiện có được liệt kê cũ → mới.
- **Trạng thái:** ✅ merge 31/10 sau review r2 PASS ([r1](../review/findings/P5.2-r1.md), [r2](../review/findings/P5.2-r2.md)). Không chặn chuyển backlog: fixture hai nguyên nhân khác feature; feature monitoring lạ nhận lookback 0 không báo lỗi; status cặp `parent_scenario` (`ambiguous_match` hay `matched` nhiều truth) — người dùng đã chốt 31/10 là `matched` nhiều truth, thực hiện ở BACKLOG-7; kẹp `scope.end_at` về `decision_at`.

### P5.0a — Bảng quyết định UQ (orchestrator, 31/10/2026)

Phân tích tĩnh theo luật đã khóa ([triage.md](triage.md) mục 2–5, [evaluation.md](evaluation.md) mục 6); chưa đo số ca, số đo thuộc P5.0b. Điều kiện `close_score >= tau_agent` **chỉ nằm trên nhánh `close` của agent**: routing `data_ticket`/`fraud_ops`/`human_review` không dùng nó, nên UQ không bao giờ đổi routing, chỉ đổi `close` ↔ `recheck`/`human_review`. Một ca chỉ để UQ đổi được quyết định khi đồng thời: (1) ca tới agent; (2) checklist benign `seasonal_calendar` (runbook benign duy nhất) đạt trên phạm vi agent đề xuất; (3) không điều kiện chuyển người nào kích hoạt; (4) agent được phép đóng (`agent_close_disabled = false`). Khi `seasonal_calendar` `close_enabled = on`, ca đạt checklist trên cùng phạm vi đã được rule đóng trước, nên (1)+(2) chỉ xảy ra khi runbook bị tắt sau calibration hoặc agent khoanh phạm vi khác (`segment_concentration`) với phạm vi của rule.

| Nhóm ([data.md](data.md) mục Kịch bản) | Tới agent? | Quyền đóng áp dụng | UQ đổi được quyết định? |
|---|---|---|---|
| `seasonal` | Chỉ khi rule không đóng: runbook tắt, checklist của rule chưa đạt, hoặc không khớp rule | `seasonal_calendar` (rule) hoặc agent qua gate | **Có**, khi runbook tắt và checklist đạt: B có thể chặn một lần đóng đúng (mất +1) |
| `legit_business_spike` | Có (không runbook benign) | Không có | Không — luôn chuyển người theo giới hạn MVP |
| `fraud_surge` | Không nếu rule `fraud_ops` đủ bằng chứng; ngược lại có | Không, trừ khi trùng ngày calendar và checklist benign đạt | **Có, hiếm**: fraud trùng ngày lịch, metric phía fraud `unavailable`/khác segment → A có thể đóng nhầm (−5), B có thể chặn |
| `data_pipeline` | Không nếu `unit_or_null_change` đạt; ngược lại có | Không (độ lớn ×100/NULL không khớp kỳ vọng lịch) | Không |
| Merchant mới, ít giao dịch | Có thể | Không: `insufficient` không bao giờ đóng | Không |
| `new_fraud` (social engineering) | Có (không rule) | Chỉ khi trùng ngày lịch và checklist đạt | **Có, hiếm**: như `fraud_surge` |
| Fraud mới + mùa vụ (`parent_scenario`) | Có | Checklist mùa vụ có thể đạt trên phần seasonal | **Có — nhóm chính**: đóng incident ứng viên của cả hai truth là unsafe ([evaluation.md](evaluation.md) mục 2); đây là nơi điều kiện score có thể giảm đóng nhầm |
| Concept drift "im lặng" | Có nếu có alert | Chỉ khi trùng ngày lịch | Hiếm |

**Kết luận (chấp nhận "không thêm giá trị"):**
- Ở vòng 1 hiện tại UQ **không thể** đổi quyết định nào: `agent_close_disabled = true`, `tau_agent = null` (`configs/triage.yaml`), agent một bước chưa có K = N = 3. So sánh 1 chỉ có nghĩa sau P4c.
- Kể cả sau P4c, so sánh 1 chỉ khác 0 trên các ca thỏa (1)–(4); nhóm có rủi ro là ca có **ngày lịch kinh doanh trùng fraud** (hai nguyên nhân, fraud trên ngày lịch). Nếu calibration/test không có ca như vậy, hoặc `seasonal_calendar` bật và agent không khoanh phạm vi khác, thì A ≡ B là kết quả phải báo cáo, không phải lỗi.
- Hệ quả cho các task sau (không đổi protocol): P5.0b phải đếm trên development số ca thỏa (1)–(4) theo nhóm truth và trạng thái `close_enabled` có thể có; P1.6 vòng 2 cần sinh được ca hai nguyên nhân fraud + mùa vụ và fraud trên ngày lịch, nếu không so sánh 1 rỗng về cấu trúc. Trên smoke hiện tại support seasonal = 0 (P3.7 hoãn tới pilot), nên P5.0b chỉ báo được giới hạn này.

### P5.0b — Bảng support trên development
- **Mục tiêu:** đo trên episode `development` (không calibration/test): số truth duy nhất theo episode và nhóm nguyên nhân; matched/ambiguous/unmatched; cửa sổ đủ; rule coverage; số ca tới agent; số ca đủ điều kiện đóng. Báo cáo private.
- **Nghiệm thu:** bảng tái lập bằng một lệnh; ghi rõ là pilot/feasibility, không tăng episode hay đổi 12/24/24.
- **Phụ thuộc:** tình trạng seasonal-volume-match / benign support theo quyết định người dùng 31/10 được ghi tại [bàn giao P3.7](p3-implementation.md).

- **Trạng thái:** ✅ merge 31/10 ([r1](../review/findings/P5.0b-r1.md), [r2](../review/findings/P5.0b-r2.md) PASS); N1–N4 không chặn chuyển vào HARDEN-1. Trên smoke development chỉ có ep-0001 clean → mọi số 0; số có nghĩa chờ pilot.

### HARDEN-1 — Guard role/lock và danh tính harness trước pilot
- **Bối cảnh:** [audit 31/10](../review/findings/AUDIT-main-31-10.md) mục A2, A3, B1, B2, C1, C5b.
- **Việc:** (1) evaluator trên harness bắt buộc `lock_identity_hashes(run-manifest)` = lock, `policy_version`/`model_slug` khớp, fingerprint policy copy = BASE; harness KHÔNG triage episode calibration/test khi chưa có lock tương ứng; (2) mọi CLI chấm/đọc truth (`evaluation decisions`, `evaluation support`, `model evaluate`, …) ngoài harness: chỉ development, hoặc qua `assert_role_access` với state dir; `--episode-role` được tôn trọng ở mọi nhánh, không lờ im lặng; (3) resume harness/BASE/policy và `triage run` so nội dung runtime/config/prompt/sampling (như lock, quyết định người dùng 31/10), giữ `git_head` làm thông tin; thông báo lỗi nói rõ hash nào lệch; (4) policy harness OpenRouter rule + agent K = 1 dùng sổ chi phí chung; CLI ngoài harness với provider thật dùng ledger bền hoặc từ chối; `create_lock` từ chối profile khác `harness-manifest.profile`, chuẩn hóa tên profile simulator ↔ lock; (5) C1: triage không import evaluation (kể cả gián tiếp qua harness), có test ranh giới import; (6) C5b: test harness không phụ thuộc Git HEAD thật.
- **Kèm P5.0b r2 N1–N4 ([findings](../review/findings/P5.0b-r2.md)):** trạng thái on chỉ so `(feature, segment)` và yêu cầu `rule.decision_at <= agent.decision_at`; hàng `by_truth_group` null kèm lý do khi trạng thái tương ứng null; sinh hàng cho mọi nhóm truth development kể cả không có match; tool error cấp runner được tính là forced-human (lưu cờ có cấu trúc hoặc giới hạn ghi trong `interpretation`); `support.py` không import hàm private của gate (đưa ra helper public), checklist thiếu `status` → `DevelopmentSupportError`.
- **Nghiệm thu:** functional test tái hiện đúng các phản ví dụ của audit (A2 chuỗi code A → commit → lock → chấm test bị từ chối; A3 `--episode-role` bị lờ, `model evaluate` in test; B1 commit docs không chặn resume; B2 lock `benchmark` từ root smoke bị từ chối); docs `data.md` §9, `evaluation.md` §9 sửa cùng thay đổi; không gọi API thật trong test.
- **Trạng thái 31/10:** chưa commit/merge. Review dự phòng Codex [r1](../review/findings/HARDEN-1-r1.md) và [r2](../review/findings/HARDEN-1-r2.md) đều `CHANGES_REQUIRED`; bốn finding r1 đã sửa. Full suite độc lập trên cây cuối: **418 passed, 1 xfailed** ([log](../review/findings/HARDEN-1-r2-tests.txt)); còn R2-1 chặn binding model artifacts với lock và R2-2 cần khôi phục một đầu vào regression đã bị thay. Đã hết hai vòng review; dừng chờ người dùng quyết định trước khi giao sửa hoặc review tiếp.

- **Trạng thái:** ✅ merge 31/10 ([r1](../review/findings/HARDEN-1-r1.md), [r2](../review/findings/HARDEN-1-r2.md), vòng sửa 3 người dùng duyệt, [r3](../review/findings/HARDEN-1-r3.md) PASS; 422 passed + 1 xfailed). `model evaluate` tạm chặn calibration/test tới HARDEN-2 (đã mở lại trong harness khi HARDEN-2 merge 31/10).

### HARDEN-2 — Ràng hash artifact model vào identity (trước pilot)
- **Bối cảnh:** [HARDEN-1 r2](../review/findings/HARDEN-1-r2.md) R2-1: `model evaluate` nạp `conformal.joblib`/metadata từ thư mục tùy ý; đổi threshold giữ nguyên version thì coverage test đổi mà lock vẫn PASS. Người dùng chọn 31/10: HARDEN-1 vòng 3 tạm chặn `model evaluate` trên calibration/test; task này mở lại đường đó.
- **Việc:** đưa content hash của artifact model thực dùng (conformal, metadata quyết định tập score/metric, provenance model) vào frozen identity/manifest; xác minh trước khi đọc truth hay ghi side effect; bỏ chặn tạm.
- **Kèm [HARDEN-1 r3](../review/findings/HARDEN-1-r3.md) NB-1/NB-2:** ngoài harness, đối chiếu role đã chọn với `episode_manifest.role` private trước khi đọc truth (relabel `episodes.role` sau khi gỡ trigger hiện chấm được truth test); xử lý có chủ đích nhánh chết `model/__main__.py:144,152-158` khi mở lại held-out.
- **Nghiệm thu:** ca đổi threshold giữ nguyên version → từ chối, không chạm truth/report/ledger; artifact đúng → chạy được sau lock.
- **Trạng thái:** ✅ merge 31/10 ([r1](../review/findings/HARDEN-2-r1.md) PASS; 423 passed + 1 xfailed). `model_artifacts` (4 file model) content-hash trong BASE và run-manifest; `model evaluate` held-out mở lại trong harness sau lock, ngoài harness chỉ development; role operational đối chiếu `episode_manifest.role` private (NB-1); NB-2 đóng. Backlog không chặn: NB-H2-1 lock chưa ràng `model_artifacts` (giả mạo đồng bộ manifest + `run_fingerprint` vẫn qua — đổi hợp đồng, cần người dùng chốt), NB-H2-2 hash rồi `joblib.load` đọc file hai lần, NB-H2-3 hard-code `"payguard-p5"` và monkeypatch vô hiệu trong test harness, NB-H2-4 report model held-out nằm ngoài `base/private/` (hỏi người dùng).

### P5.0c — Report shell khóa trước calibration
- **Mục tiêu:** cố định bảng/biểu đồ README trước khi chạm calibration, theo [evaluation.md](evaluation.md) mục 4–8: T1 kết quả chính theo policy (4 baseline + nhánh K = N = 1): unsafe truth rate `x/n` [CI], unsafe episode rate, detection recall, timely routing (tách tự động / `human_review`), benign automation, workload (case/recheck/ticket/incident con), USD; T2 chênh lệch ghép cặp hybrid-UQ − rule+human, bootstrap theo episode cùng chỉ số resample (mục 7); T3 so sánh 1 — từng ca A ≠ B theo nhóm truth; T4 so sánh 2 — K = 1 vs K = 3 cạnh USD; T5 nhánh agent theo nguồn, known/unknown, benign không runbook; T6 matched/ambiguous/unmatched theo policy + cờ cận dưới quan sát; F1 risk–coverage theo tau (chỉ calibration), F2 chi phí–chất lượng.
- **Nghiệm thu:** (1) một lệnh sinh T1–T6 + F1–F2 từ harness P5.1 với fake provider trên development; mẫu số 0 → `NA`, mẫu toàn 0 ghi rõ bootstrap suy biến; (2) functional test: bootstrap ghép cặp dùng cùng chỉ số resample cho mọi policy và resample nguyên episode; (3) code report nằm trong phạm vi hash của lock `prompt-rule` (đổi report sau lock bị phát hiện); (4) không đọc calibration/test. Nhánh chưa có (K = 3, UQ) hiện `NA` có lý do, không bịa số.

### HARNESS-PILOT — Harness chạy profile pilot + tối ưu chẩn đoán (người dùng duyệt 02/11/2026)
- **Bối cảnh:** `payguard run` chỉ nhận `--profile smoke` và tự sinh dữ liệu; sinh pilot mất ~114 phút, trong đó ~79 phút là `_p14_feature_diagnostics` O(N²) ([P1.8 r1](../review/findings/P1.8-r2-r1.md) F4). P4.6 ca thứ ba, P5.0b chạy lại và P5.0d đều cần run dir pilot.
- **Việc:** (1) **F4:** tối ưu `_p14_feature_diagnostics` mà kết quả không đổi (lọc giao dịch theo episode/`base` trước T0 bằng SQL, parse timestamp một lần, duyệt theo thời gian giữ đúng as-of theo `available_at`, chỉ mục tăng dần thay cho quét lặp, tái dùng lịch sử `base`). (2) `payguard run --profile pilot`: sinh pilot qua `simulator generate --profile pilot`, profile nằm trong identity/manifest (root smoke và pilot không dùng lẫn); giữ nguyên guard HARDEN-1/2 — trước lock chỉ triage và chấm `development`, sổ chi phí/role dùng chung như P5.1. (3) Ghi trong mục P5.1 lệnh dựng run dir pilot cho `triage measure` (không chạy LLM thật).
- **Nghiệm thu:** smoke: `reports/smoke.json`, `private/reports/smoke_truth.json` SHA-256 không đổi (`3d1703e1…`, `113f15e9…`) và output harness smoke giống main; pilot: fingerprint dữ liệu `bd463217…a443c6`, `private/reports/pilot_truth.json` giống hệt `/tmp/p18-pilot` (hoặc chứng minh bằng so sánh trên subset 4 episode nếu so toàn bộ quá lâu), pha báo cáo pilot ≤ 10 phút; `payguard run --profile pilot --policy rule_human --out <root>` chạy được một lần với state dir tạm, `--analysis-role calibration|test` khi chưa lock bị từ chối; functional test qua CLI cho profile pilot (subset nhỏ nếu cần), không gọi API thật. Không xem metric/alert/score của calibration/test.
- **Kèm P1.8 r1 (không chặn, làm nếu gọn):** F2 validator kiểm `profile`/`report_version` báo cáo private; F3 check held-out quét đệ quy ID calibration/test + khóa kiểu điểm, có test.

### P5.0d — Khả thi chi phí pilot 60
- **Mục tiêu:** (1) dùng số incident tới agent mỗi episode development từ P5.0b, theo nhóm truth, gồm cả episode clean (alert nền); (2) **một lượt thật nhỏ** (người dùng duyệt 31/10): agent một bước hiện tại qua OpenRouter trên 1–2 incident development, ghi token/lượt, recheck; chi phí qua sổ chi phí bền P5.1, tính vào trần 10 USD; K = N = 3 thật hoãn tới P4c; (3) bảng dự báo chi phí pilot 60 cho từng nhánh (rule + agent, K = 1, K = 3 ngoại suy) với khoảng lạc quan/bi quan, so với trần 10 USD.
- **Nghiệm thu:** tái lập bằng một lệnh (phần dự báo; lượt thật chạy đúng một lần, kết quả lưu private); chỉ development; ghi rõ là feasibility; không đổi 12/24/24 hay số episode. Dự báo vượt trần → báo người dùng, không tự đổi phạm vi.

### P5.1 — Harness tối thiểu và sổ chi phí bền
- **Mục tiêu:** một lệnh chạy dữ liệu → model → monitoring → alert → triage → báo cáo (MASTER mục 1). Base đóng băng sau scores/metrics/alerts, trước mọi incident/decision; mỗi policy chạy trên bản copy operational riêng; incident tính lại theo lịch sử decision riêng.
- **Danh tính run:** manifest gồm hash code/config/prompt/sampling/base; kiểm tra trước resume, lệch → dừng. Sổ seed/role: development được phân tích, calibration chỉ sau khi khóa prompt/rule, test chỉ sau khi khóa policy.
- **Sổ chi phí:** ledger bền cộng mọi lời gọi (mọi nhánh, retry, recheck, lần chạy trước restart); chặn trước khi vượt trần đã khóa. **Người dùng chốt 31/10/2026 (review P5.1 r1, finding 2):** sổ chi phí và sổ role/lock nằm ở **một đường dẫn cố định ngoài mọi benchmark root** (và ngoài mọi DB/file tool agent đọc được), dùng chung cho mọi root, mọi lần đổi code; trần 10 USD và lock calibration/test áp cho toàn benchmark.
- **Nghiệm thu:** fingerprint input giống nhau giữa các policy và không đổi sau chạy; resume idempotent; fake provider end-to-end; truth chỉ evaluator đọc; commit trước tool giữ như P4.4b.

- **Trạng thái:** ✅ merge 31/10 ([r1](../review/findings/P5.1-r1.md), [r2](../review/findings/P5.1-r2.md); vòng sửa 3 người dùng duyệt, orchestrator chạy full test 371 passed thay cho review vòng 3).

### P5.1 — CLI, locks, ngân sách và identity

Smoke workflow ví dụ dùng state directory tạm để tránh tạo trạng thái benchmark thật:

```bash
export PAYGUARD_BENCHMARK_STATE_DIR="$(mktemp -d)"
python -m payguard run --profile smoke --policy rule_human --out data/p5-smoke
python -m payguard run --profile smoke --policy rule_agent_fake --out data/p5-smoke
```

Lệnh thứ hai tái dùng BASE đã freeze; mỗi policy có manifest và operational DB copy riêng. Evaluator report của mỗi policy được giữ dưới `base/private/reports/p5_1/<policy>/`, tránh đặt truth-derived report trong đường dẫn agent/tool. Mặc định chỉ evaluator report trên `development`. Muốn phân tích `calibration` hoặc `test`, thêm `--analysis-role calibration|test`; role ledger ghi lại tập episode và usage. `triage measure` vẫn là mode riêng.

Khóa trước khi phân tích held-out role bằng profile tương ứng:

```bash
python -m payguard lock prompt-rule --profile smoke --out data/p5-smoke
python -m payguard lock policy --profile smoke --policy rule_agent_fake --out data/p5-smoke
```

`prompt-rule` dùng chung cho các policy trong một profile; `policy` khóa riêng theo policy. Lock marker ghi thời điểm UTC và hash nội dung runtime code, configs, prompt sources và sampling; không ghim Git HEAD/dirty flag. Policy lock còn gắn `policy_version` và `model_slug`. Lệnh lock từ chối khi Git working tree có thay đổi chưa commit; file Git-ignore không được Git tính là thay đổi. Marker bất biến và idempotent khi identity khớp; identity khác không ghi đè lock, và tạo benchmark root mới cũng không reset lock. Smoke và benchmark có lock độc lập theo profile. Calibration cần prompt/rule lock; test cần policy lock; guard chạy trước evaluator đọc truth.

Vị trí state directory, binding trong harness manifest và schema cost/role/lock ledger được định nghĩa tại [data.md §9](data.md#9-p51-harness-artifacts). Cost ledger append-only dùng reservations/settlements cùng file lock; reservation chưa settle sau crash vẫn tính vào ngân sách. LLM client reserve ước lượng trần input/output theo payload và sampling trước từng lần gọi, gồm retry; khi không đủ ngân sách thì không gửi request. Fake provider ghi chi phí 0. `configs/llm.yaml` giữ nguyên cap $1 cho P4; harness dùng cap benchmark $10 từ `configs/benchmark.yaml`, không cộng hai cap thành $11. Mọi lần reserve trong ledger phải ghi cùng cap; cap khác bị từ chối. Settlement cao hơn reservation được ghi là overrun trong ledger và policy report.

`--profile pilot` dùng cùng harness: BASE được sinh bằng `simulator generate --profile pilot` bên trong root. Root ghi `profile` vào `harness-manifest.json`, `base/generation-manifest.json` và `base/base-manifest.json`; root smoke và pilot không dùng lẫn, lock pilot nằm ở namespace `benchmark`. Trước lock, harness chỉ triage và chấm `development`; `--analysis-role calibration|test` bị từ chối khi lock tương ứng chưa có.

Lệnh dựng run dir pilot cho `triage measure` (agent `fake`, không gọi LLM thật): chép BASE đã đóng băng sang thư mục standalone nằm ngoài mọi harness root, rồi đo:

```bash
python -m payguard run --profile pilot --policy rule_human --out data/p5-pilot
mkdir -p data/p5-pilot-measure/operational
cp data/p5-pilot/base/operational/observations.sqlite data/p5-pilot-measure/operational/
python -m triage measure --run data/p5-pilot-measure --agent-provider fake --incidents 3 --out data/p5-pilot-measure/measure.json
```

Run dir này chưa có agent decision và chỉ dùng cho một lần đo. `measure` đọc `development` trước; nếu thiếu ca đủ điều kiện thì fallback sang `calibration` bị lock guard từ chối trước khi lock tồn tại.

Standalone `python -m triage run`/`measure` trên policy directory xác nhận state directory, current code/config/prompt/sampling hashes, BASE fingerprint và policy identity trước khi thao tác DB hoặc ghi output. Khi resume, decision rows hiện có phải giữ đúng `policy_version`; agent rows cũng phải đúng `model_slug` trong manifest. Lệch identity hoặc mixed rows thì từ chối với exit 2. Harness root hiện không có process lock: chạy tuần tự các lệnh trên cùng root. Chi tiết CLI evaluator và triage được mô tả tại [evaluation.md, mục 9](evaluation.md#9-cli-cham-decisions-va-chon-cap-p45-p51) và [triage.md, mục 9](triage.md#9-cli-trong-p51-harness).
