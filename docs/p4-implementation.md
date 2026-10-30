# P4 — Sub-plan triển khai triage (rule, gate, agent)

> Trạng thái: **người dùng duyệt 26/10/2026; P4.4b vòng 2 đã sửa 2 finding chặn review r1 và cập nhật replay as-of; P4.6 round 1 đo thật trên 2 ca theo quyết định người dùng 29/10; ca thứ ba hoãn tới pilot 60 P1 vòng 2**. Hợp đồng triage: [triage.md](triage.md); chấm điểm: [evaluation.md](evaluation.md); schema: [data.md](data.md). File này sở hữu thứ tự task và nghiệm thu P4; không chép hợp đồng.
> Format task và luật hai vòng giống [p1-implementation.md](p1-implementation.md). Điều phối: [orchestration.md](orchestration.md). Mốc ★ và ngân sách LLM: [MASTER_PLAN.md](../MASTER_PLAN.md) mục 3 và 5.

## Thứ tự hai vòng

Vòng 1 chỉ đủ cho mốc ★: snapshot → score → một monitor → **một rule → gate → `decisions` → evaluator**; thêm **agent một bước (1 SQL, không UQ)**; đo token trên 3 case.

| Task | Vòng 1 — luồng mỏng tới ★ | Vòng 2 (P4a/P4b/P4c đầy đủ) |
|---|---|---|
| P4.1 `decisions` + gate — vòng 1 hoàn tất ✅ | Schema `decisions`, gate phân nhánh theo hành động, `evidence_status` do code tính | Recheck tối đa 2 lần cách 1 ngày; xung đột nhiều rule |
| P4.2 Rule — vòng 1 hoàn tất ✅ | `unit_or_null_change` → `data_ticket`; checklist operational đúng episode và đúng `available_at` của snapshot incident; fixture từng nhánh và smoke đều đạt | Đủ 4 runbook; `seasonal_calendar` với kỳ vọng có trọng số; `close_enabled` chọn trên calibration |
| P4.3 Vòng lặp quyết định ↔ incident | Gate đọc incident qua `read_as_of`, ghi `decisions`, monitoring nhận qua `DecisionSource` | — |
| P4.4 Agent một bước | 1 lượt, 1 `run_sql`, không UQ; agent không được đóng (`agent_close_disabled`). **P4.4a đã bàn giao; P4.4b vòng 2 đã sửa finding chặn review r1**. | K = 3, `run_sql` N = 3 + cache, `close_score`, `tau_agent` trên calibration, bộ câu hỏi + DB phản ví dụ |
| P4.5 Evaluator cho `decisions` — vòng 1 đã bàn giao ✅ | Timely routing recall, unsafe truth rate, chấm theo `decision_at`; CLI đọc decisions từ DB operational read-only; báo cáo private | Utility, baseline, bootstrap theo episode (P5) |
| P4.6 Đo token | 3 incident cố định, ghi token/chi phí thật vào MASTER mục 5 | Khóa trần USD → số episode |

## Bố trí file dự kiến

```text
triage/       __init__.py, gate.py, runbooks.py, decisions.py, tools.py, agent.py, llm.py, __main__.py
evaluation/   decisions.py                         # chấm decisions; vẫn là module duy nhất đọc truth
configs/      triage.yaml                          # ngưỡng runbook khởi đầu + provenance, policy_version
tests/        test_triage_gate.py, test_triage_runbooks.py, test_triage_agent.py, test_evaluation_decisions.py
```

## Các task

### P4.1 — Bảng `decisions` và gate
- **Mục tiêu:** hiện thực bảng `decisions` ([triage.md](triage.md) mục 6) và gate phân nhánh theo hành động (mục 2).
- **Phụ thuộc:** schema `incidents` của P3.3. Gate nhận incident qua interface trong bộ nhớ; đọc từ DB operational (P3.5) nối ở P4.3, nên P4.1 chạy song song được với P3.5.
- **Kiểm tra:** `evidence_status` chỉ do code tính từ output tool/checklist, LLM không ghi được trường này; `data_ticket`/`fraud_ops` không đòi benign hay score; `close` chỉ qua runbook `close_enabled` hoặc agent qua tau; điều kiện bắt buộc chuyển người thắng mọi nhánh; tool lỗi/thiếu bằng chứng → `human_review`. Mỗi lần gate chạy ghi một dòng có `policy_version`, `as_of_time`, kết quả từng mục checklist.
- **Hoàn tất:** schema `decisions` ở [data.md](data.md) (operational, không truth) + test từng nhánh của mục 2.

### P4.2 — Runbook `unit_or_null_change`
- **Mục tiêu:** checklist theo [triage.md](triage.md) mục 3 cho đổi đơn vị/NULL hàng loạt → đề xuất `data_ticket`.
- **Phụ thuộc:** P4.1.
- **Kiểm tra:** ngưỡng khởi đầu trong `configs/triage.yaml` có provenance, KHÔNG chỉnh trên episode test; chưa đủ 1 ngày sau điểm đổi → `insufficient`; có signal fraud cùng segment → chuyển người. Mọi đầu vào operational đọc bằng `read_as_of` theo đúng episode và `as_of_time` của snapshot incident.
- **Cửa sổ bằng chứng:** chỉ xét transition có ngày sau nằm trong khoảng từ `window_start` nhỏ nhất tới `window_end` lớn nhất của các metric nguồn được nối từ `alert_ids_json` của snapshot incident; nếu không đọc được metric nguồn thì bằng chứng `insufficient`. Không suy cửa sổ chỉ từ `opened_at`.
- **Giới hạn signal fraud vòng 1 (quyết định người dùng 27/10):** kiểm tra signal fraud hiện có trong alerts/metrics operational tại snapshot bằng allowlist tường minh `fraud_side_metrics` trong `configs/triage.yaml`; chỉ alert có metric ID được allowlist mới là signal. Chuyển ra 24 h, người gửi lần đầu, số tiền sát 10 triệu và `review_rate` chưa có reference phải hiện thành `unavailable` cùng lý do trong checklist. `unavailable` không phải bằng chứng không có fraud và không chặn `data_ticket`; signal fraud khả dụng cùng segment vẫn bắt buộc chuyển người. Vòng 1 không thêm metric fraud mới.
- **Smoke:** 2 incident `data_pipeline` được gate routing `data_ticket`; incident clean/seasonal có checklist `not_applicable` để P4.4b chuyển agent. Vòng 1 không bật `close_enabled`.
- **Ghi chú nghiệm thu smoke (quyết định người dùng 30/10/2026):** với mỗi episode `data_pipeline`, test phải tìm incident `amount_avg_7d` đầu tiên có source window chứa bước nhảy amount tại ngày bắt đầu injector; checklist phải `pass` và gate phải `data_ticket`. Có đúng một incident như vậy trong từng episode smoke pipeline (2 tổng cộng). Incident amount về sau không có adjacent jump có thể là `not_applicable`, nhưng gate phải `human_review` và không bao giờ `close`. Assertion `not_applicable` cho episode clean/seasonal được giữ nguyên. Thay đổi này chỉ điều chỉnh kiểm tra trong `tests/test_triage_runbooks.py`; không đổi triage runtime.
- **Kết quả pytest 30/10/2026:** pipeline assertions trên đạt; suite vẫn dừng ở assertion non-pipeline được giữ nguyên. Clean `ep-0001` có cửa sổ 1–7/2 với card median 6,282 → 3,257,620 VND (ratio 518.56), nên checklist trả `pass`. Không sửa assertion hoặc triage runtime theo quyết định giữ non-pipeline check.
- **Ghi chú false positive và quyết định người dùng 30/10/2026:** nguyên nhân là `unit_or_null_change` chưa có minimum-transaction-support check, không phải lỗi world P1.3. Incident của clean `ep-0001` dùng segment `all`; trong cửa sổ 1–7/2, số giao dịch card mỗi ngày là 18, 1, 1, 0, 2, 0, 1, trong đó card thường tương ứng là 1, 1, 1, 0, 2, 0, 1. Ngày 1 có thêm 17 dòng card-testing/control của P1.4 (8 lần thử, 1 follow-up được chấp thuận, 8 lần control), làm median card là 6,282 VND; ngày 2 chỉ có một giao dịch card thường, 3,257,620 VND. Rule so median card trong cùng segment `all`, nhận ratio 518.56; ngưỡng cấu hình 1,000 với dải nhân 2 chấp nhận ratio [500, 2,000], dù mỗi ngày chỉ có một giao dịch card thường. Cấu hình giao dịch thường dùng cùng phân phối amount uniform 1,000–100,000,000 VND cho mọi channel; trọng số channel QR/wallet/card lần lượt 0.50/0.30/0.20 và kỳ vọng 0.15 giao dịch/customer/ngày. Với 20 customer smoke, kỳ vọng card thường là 0.6/ngày (4.2/tuần); quan sát 6 giao dịch card thường trong tuần là hợp lý. Trong dữ liệu được sinh ở cửa sổ này, QR n=9 có min/median/max 8,631,368/65,056,287/87,420,606 VND; wallet n=11 có 10,185,386/61,295,385/98,686,996 VND; card n=23 có 2,627/8,561/83,414,385 VND, gồm 17 dòng P1.4. Riêng 6 giao dịch card thường có median 28,908,294.5 VND và đều trong khoảng cấu hình. Theo quyết định người dùng, chỉ assertion case này được tách thành `xfail(strict=True)` với lý do `unit_or_null_change lacks minimum transaction support; fixed by BACKLOG-5`; assertion vẫn giữ nguyên, các case non-pipeline khác vẫn assert `not_applicable`. Không sửa `triage/`; hỗ trợ minimum transaction support thuộc BACKLOG-5.
- **Đính chính smoke review fix vòng 1 (30/10/2026):** hai pipeline episode có tổng 5 data-ticket decisions trong smoke: ba incident amount khớp bước nhảy thật (`ep-0002/incident-ea21a6a3c09cbde42c6a40b1`, `ep-0004/incident-553672f4a8f9fcd91ee734d2`, `ep-0004/incident-7cfa5401d22e49b6c3a7faa1`), một coincidence support thấp (`ep-0002/incident-e346f09c6706875fcc708684`, QR count n=3→n=1), và false positive clean (`ep-0001/incident-c89881b3dc8c2d6e9ebd1354`, BACKLOG-5). Test khóa rõ tập ID này và cho phép đúng false positive đã biết trên clean/seasonal; không có data-ticket clean/seasonal nào khác. Dòng Smoke phía trên ghi 2 incident pipeline mô tả template count, không phải tổng quyết định data-ticket.
- **Hoàn tất:** runbook + test trên fixture và smoke. Các runbook còn lại ở vòng 2; vòng 1 KHÔNG bật `close_enabled` cho runbook nào.

### P4.3 — Vòng lặp quyết định ↔ incident
- **Mục tiêu:** chạy gate theo ngày nghiệp vụ trên incident đọc qua `read_as_of`; quyết định ghi vào `decisions` và quay lại monitoring qua `DecisionSource` để tạo incident con đúng [monitoring.md](monitoring.md) mục 2.
- **Phụ thuộc:** P4.1, P4.2.
- **Kiểm tra:** tại t gate chỉ thấy incident/alert có `available_at <= t`; quyết định cuối không mở lại incident; chạy lại → `decisions` y hệt.
- **Độ trễ snapshot đã chốt:** ngày `d`, monitoring ghi snapshot incident đang mở tại `as_of_time(d)`, gate đọc snapshot đó qua `read_as_of`, rồi mới ghi decision với `decision_at = available_at = as_of_time(d)`. Vì monitoring ngày `d` chạy trước gate, `DecisionSource` chỉ áp decision này ở lần monitoring kế tiếp (snapshot ngày nghiệp vụ `d+1`); incident chuyển trạng thái tại snapshot `d+1`, và alert từ snapshot đó trở đi có thể tạo incident con.
- **Mặc định agent:** incident không khớp runbook được chuyển qua interface agent; tới khi P4.4b nối agent, fallback là `human_review` với lý do `agent_not_configured`. `evidence_status` vẫn do gate tính từ checklist/tool output.
- **Hoàn tất:** CLI `python -m triage run` cho smoke.
- **Trạng thái:** vòng 1 hoàn tất 27/10/2026. CLI tiêu thụ `daily_metrics` và `alerts` đã có trong operational DB, chạy incident grouping tăng dần theo snapshot; acceptance, replay và validate đạt.

- **Backlog P4.3 (không chặn, [findings r2](../review/findings/P4.3-r2.md)):** test replay ở `tests/test_simulator_cli.py:285` ghi đè `before` sau lần chạy lại. **Đã sửa ở BACKLOG-1 (28/10).**

### P4.4 — Agent một bước
- **Mục tiêu:** incident rule không xử lý được → một lượt agent gọi một `run_sql` (không UQ), đề xuất nguyên nhân/hành động; gate quyết định.
- **Phụ thuộc:** P4.3. Chia hai phần để chạy song song: **P4.4a** (client LLM + tool `run_sql`, chỉ phụ thuộc P3.5/P1.2) làm trước song song với P4.1–P4.3; **P4.4b** (vòng agent nối gate) sau P4.3.
- **P4.4a:** `triage/llm.py` — client OpenRouter đọc `OPENROUTER_API_KEY` từ môi trường/`.env`, model và sampling (gồm cấu hình reasoning) từ `configs/llm.yaml` (file riêng để không đụng `configs/triage.yaml` của P4.1), ghi canonical slug + token + chi phí + độ trễ cho mỗi lần gọi, kiểm tra JSON, lỗi/rỗng/hết token/hết ngân sách → kết quả lỗi có cấu trúc (không ném ra ngoài), trần chi phí cộng dồn; client giả lập tất định cho test. `triage/tools.py` — `run_sql(question)` sinh 1 SQL qua client, chỉ chạy SELECT trên kết nối SQLite read-only tới DB operational, chỉ thấy view as-of của đúng episode tại `as_of_time` (dựng từ `read_as_of`), không thấy bảng/đường dẫn private; giới hạn số dòng và thời gian chạy; mỗi lần gọi có `tool_trace_id`.
- **Kiểm tra:** tool chỉ có kết nối read-only tới DB operational qua as-of, không nhìn thấy đường dẫn hay bảng private; agent không được `close` ở vòng 1; LLM sai format/tool lỗi/hết ngân sách → `human_review`. Test dùng LLM giả lập tất định; gọi API thật chỉ ở P4.6.
- **Hoàn tất:** trace tool có ID trong `tool_trace_ids`; token/chi phí ghi vào `decisions`.
- **P4.4a vòng 1 đã bàn giao (review r2 PASS, 27/10):** client OpenRouter + `run_sql` read-only as-of, test offline. Backlog vòng 2 (không chặn): kết quả BLOB bị báo `snapshot_error` và mất SQL trong trace ([findings r2](../review/findings/P4.4a-r2.md)). **Đã sửa ở BACKLOG-1 (28/10):** BLOB → lỗi có cấu trúc `sql_unsupported_type`, giữ SQL trong trace.
- **P4.4b vòng 1 đã bàn giao (28/10), vòng 2 sửa finding chặn review r1, vòng 3 thêm fail-fast provider:** agent gọi một `run_sql` rồi một proposal JSON; CLI mặc định `UnconfiguredAgent`, lựa chọn `fake` tất định, OpenRouter chỉ khi chọn rõ `--agent-provider openrouter`. Gate giữ `agent_close_disabled`, trace/usage/model slug được ghi vào decisions. Rule decisions giữ `model_slug = NULL`; run_sql thấy incident đã commit; mỗi run directory dùng một agent provider và runner từ chối đổi provider khi đã có agent decision. Smoke có 2 `data_pipeline` qua rule và các incident còn lại qua fake agent; P4.5 tạo báo cáo private. Findings không chặn 3–4 vẫn để backlog. Finding 3 (evaluator trộn nhiều cặp provider): **Đã sửa ở BACKLOG-6 (30/10).** Finding 4 (trần ngân sách tính theo tiến trình) vẫn để backlog.

### P4.5 — Evaluator cho `decisions`
- **Mục tiêu:** chấm `decisions` theo [evaluation.md](evaluation.md) mục 3–4: timely routing recall, unsafe truth rate, dangerous closure rate.
- **Phụ thuộc:** P4.3, P3.4.
- **Kiểm tra:** truth của quyết định = sự cố có `effect_available_at <= decision_at` và giao phạm vi; `close` không bao giờ đạt routing; mẫu số 0 → NA; unmatched không thành benign. Triage KHÔNG import evaluator.
- **Hoàn tất:** bảng kết quả smoke (dù còn xấu) trong thư mục private.
- **Nối DB vòng 1 đã bàn giao (28/10):** `python -m evaluation decisions --run <run-dir>` mở DB operational read-only, dùng các episode operational và ghi report dưới private.

- **Backlog P4.5 (không chặn, [findings r1](../review/findings/P4.5-r1.md)):** thêm test riêng cho `effect_available_at <= decision_at < intervention_start_at` và hai biên intervention/deadline. **Đã sửa ở BACKLOG-3 (28/10).**

### P4.6 — Đo token trên 3 case
- **Mục tiêu:** chạy agent một bước với LLM thật trên 3 incident cố định, ghi token và chi phí.
- **Phụ thuộc:** P4.4. **Cần người dùng:** API key và trần chi phí tạm (MASTER mục 5).
- **Trạng thái 29/10/2026:** CLI, report public-safe, offline tests và một lượt OpenRouter thật đã hoàn tất. Theo quyết định người dùng, lượt này đo 2 incident thay vì 3: `ep-0001` (development) và `ep-0003` (calibration); ca thứ ba hoãn tới pilot 60 P1 vòng 2. Số đo bốn call được ghi trong [MASTER_PLAN.md](../MASTER_PLAN.md) mục 5.
- **CLI:** `python -m triage measure --run <run_dir> --agent-provider {fake,openrouter} --incidents N --out <report.json>`. `--incidents` mặc định 3; thiếu incident đủ điều kiện → dừng trước mọi lời gọi LLM (`insufficient_agent_eligible_incidents`), muốn đo ít hơn phải truyền N tường minh. Chọn tất định theo (`episode_id`, `as_of_time` đầu tiên, `incident_id`) từ `development`, chỉ dùng `calibration` khi `development` thiếu, không bao giờ `test`; danh sách chọn ghi vào report trước lời gọi đầu. Mỗi incident chạy agent một bước đúng một lần (2 lời gọi), không recheck. Report public-safe gồm `selected_incidents`, `calls` (token, chi phí, độ trễ, slug, lỗi), `incident_totals`, `totals`, `extrapolation`, `status`/`error_status`. Run dir phải chưa có agent decision; sau `measure` run dir chỉ dùng một lần (không chạy tiếp `triage run` trên đó). Measure chỉ lưu snapshot incident tới `as_of_time` lớn nhất trong các ca đã chọn; snapshot sau mốc đó chỉ tồn tại trong transaction để kiểm tra ứng viên rồi bị rollback.
- **Hoàn tất:** số token thật trong MASTER mục 5; mốc ★ đạt.
- **Backlog P4.6 (không chặn, [findings r1](../review/findings/P4.6-r1.md)):** comment `triage/agent.py:178` nói lỗi LLM ở bước SQL chỉ áp dụng cho measurement, thực tế áp dụng mọi đường gọi → sửa comment; `measure` lưu incident cho mọi snapshot dev/cal → chỉ lưu tới `as_of_time` lớn nhất đã chọn để run dir dùng tiếp được. **Đã sửa ở BACKLOG-4 (29/10).** **Phạm vi thật (review BACKLOG-4 r1, người dùng chốt 29/10):** bản sửa chỉ cắt snapshot theo `as_of_time` lớn nhất chung của các ca đã chọn, không theo từng episode, nên chỉ giảm snapshot thừa; khi các ca chọn lệch ngày vẫn có thể còn snapshot `open` mâu thuẫn với decision đã ghi. Run dir sau `measure` vẫn chỉ dùng một lần như mục CLI.

## Câu hỏi cần người dùng chốt trước khi code

1. **`decision_at` — đã chốt (người dùng giao planner chọn, 26/10):** vòng 1 đặt `decision_at = as_of_time` của snapshot, không cộng độ trễ xử lý giả định. Độ trễ thật (gồm thời gian gọi LLM) ghi riêng trong cột token/chi phí, không đổi `decision_at`.
2. **LLM — đã chốt (người dùng, 26/10):** Qwen 3.7 Flash qua OpenRouter, đọc `OPENROUTER_API_KEY` từ môi trường/`.env` (không commit, không log key). Chi tiết model và hệ quả (không có JSON schema chặt): [rationale.md](rationale.md) mục 3. Trần chi phí tạm: **1 USD** cho toàn bộ P4 vòng 1 (người dùng chốt 26/10).
3. **Rule vòng 1:** chọn `unit_or_null_change` (smoke có 2 sự cố `data_pipeline` được phát hiện) thay vì `seasonal_calendar` (2 incident seasonal hiện unmatched do hiệu ứng trễ cửa sổ — cần matcher vòng 2 trước).

## Nghiệm thu P4 vòng 1
- [x] Gate đúng từng nhánh của [triage.md](triage.md) mục 2; `evidence_status` do code tính.
- [x] Một rule + agent một bước chạy end-to-end trên smoke; không tool nào đọc được truth.
- [x] Evaluator chấm `decisions` theo `decision_at`; bảng kết quả smoke được ghi private.
- [x] Token thật của 3 case ghi vào MASTER mục 5. **Ghi chú 29/10 (quyết định người dùng):** đo 2 case vì smoke chỉ có 2 incident tới agent ngoài test; ca thứ ba hoãn tới pilot 60 P1 vòng 2.
- [x] Test runtime xanh.
