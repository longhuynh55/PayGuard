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

Standalone `python -m triage run`/`measure` trên policy directory xác nhận state directory, current code/config/prompt/sampling hashes, BASE fingerprint và policy identity trước khi thao tác DB hoặc ghi output. Khi resume, decision rows hiện có phải giữ đúng `policy_version`; agent rows cũng phải đúng `model_slug` trong manifest. Lệch identity hoặc mixed rows thì từ chối với exit 2. Harness root hiện không có process lock: chạy tuần tự các lệnh trên cùng root. Chi tiết CLI evaluator và triage được mô tả tại [evaluation.md, mục 9](evaluation.md#9-cli-cham-decisions-va-chon-cap-p45-p51) và [triage.md, mục 9](triage.md#9-cli-trong-p51-harness).
