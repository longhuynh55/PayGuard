# P4 — Sub-plan triển khai triage (rule, gate, agent)

> Trạng thái: **người dùng duyệt 26/10/2026; vòng 1 đang triển khai**. Hợp đồng triage: [triage.md](triage.md); chấm điểm: [evaluation.md](evaluation.md); schema: [data.md](data.md). File này sở hữu thứ tự task và nghiệm thu P4; không chép hợp đồng.
> Format task và luật hai vòng giống [p1-implementation.md](p1-implementation.md). Điều phối: [orchestration.md](orchestration.md). Mốc ★ và ngân sách LLM: [MASTER_PLAN.md](../MASTER_PLAN.md) mục 3 và 5.

## Thứ tự hai vòng

Vòng 1 chỉ đủ cho mốc ★: snapshot → score → một monitor → **một rule → gate → `decisions` → evaluator**; thêm **agent một bước (1 SQL, không UQ)**; đo token trên 3 case.

| Task | Vòng 1 — luồng mỏng tới ★ | Vòng 2 (P4a/P4b/P4c đầy đủ) |
|---|---|---|
| P4.1 `decisions` + gate — vòng 1 hoàn tất ✅ | Schema `decisions`, gate phân nhánh theo hành động, `evidence_status` do code tính | Recheck tối đa 2 lần cách 1 ngày; xung đột nhiều rule |
| P4.2 Rule | Một runbook: `unit_or_null_change` → `data_ticket` | Đủ 4 runbook; `seasonal_calendar` với kỳ vọng có trọng số; `close_enabled` chọn trên calibration |
| P4.3 Vòng lặp quyết định ↔ incident | Gate đọc incident qua `read_as_of`, ghi `decisions`, monitoring nhận qua `DecisionSource` | — |
| P4.4 Agent một bước | 1 lượt, 1 `run_sql`, không UQ; agent không được đóng (`agent_close_disabled`) | K = 3, `run_sql` N = 3 + cache, `close_score`, `tau_agent` trên calibration, bộ câu hỏi + DB phản ví dụ |
| P4.5 Evaluator cho `decisions` | Timely routing recall, unsafe truth rate, chấm theo `decision_at` trên smoke | Utility, baseline, bootstrap theo episode (P5) |
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
- **Kiểm tra:** ngưỡng khởi đầu trong `configs/triage.yaml` có provenance, KHÔNG chỉnh trên episode test; chưa đủ 1 ngày sau điểm đổi → `insufficient`; có signal fraud cùng segment → chuyển người. Smoke: 2 incident `data_pipeline` được routing `data_ticket`; incident clean/seasonal không khớp → chuyển agent.
- **Hoàn tất:** runbook + test trên fixture và smoke. Các runbook còn lại ở vòng 2; vòng 1 KHÔNG bật `close_enabled` cho runbook nào.

### P4.3 — Vòng lặp quyết định ↔ incident
- **Mục tiêu:** chạy gate theo ngày nghiệp vụ trên incident đọc qua `read_as_of`; quyết định ghi vào `decisions` và quay lại monitoring qua `DecisionSource` để tạo incident con đúng [monitoring.md](monitoring.md) mục 2.
- **Phụ thuộc:** P4.1, P4.2.
- **Kiểm tra:** tại t gate chỉ thấy incident/alert có `available_at <= t`; quyết định cuối không mở lại incident; chạy lại → `decisions` y hệt.
- **Hoàn tất:** CLI `python -m triage run` cho smoke.

### P4.4 — Agent một bước
- **Mục tiêu:** incident rule không xử lý được → một lượt agent gọi một `run_sql` (không UQ), đề xuất nguyên nhân/hành động; gate quyết định.
- **Phụ thuộc:** P4.3. Chia hai phần để chạy song song: **P4.4a** (client LLM + tool `run_sql`, chỉ phụ thuộc P3.5/P1.2) làm trước song song với P4.1–P4.3; **P4.4b** (vòng agent nối gate) sau P4.3.
- **P4.4a:** `triage/llm.py` — client OpenRouter đọc `OPENROUTER_API_KEY` từ môi trường/`.env`, model và sampling (gồm cấu hình reasoning) từ `configs/llm.yaml` (file riêng để không đụng `configs/triage.yaml` của P4.1), ghi canonical slug + token + chi phí + độ trễ cho mỗi lần gọi, kiểm tra JSON, lỗi/rỗng/hết token/hết ngân sách → kết quả lỗi có cấu trúc (không ném ra ngoài), trần chi phí cộng dồn; client giả lập tất định cho test. `triage/tools.py` — `run_sql(question)` sinh 1 SQL qua client, chỉ chạy SELECT trên kết nối SQLite read-only tới DB operational, chỉ thấy view as-of của đúng episode tại `as_of_time` (dựng từ `read_as_of`), không thấy bảng/đường dẫn private; giới hạn số dòng và thời gian chạy; mỗi lần gọi có `tool_trace_id`.
- **Kiểm tra:** tool chỉ có kết nối read-only tới DB operational qua as-of, không nhìn thấy đường dẫn hay bảng private; agent không được `close` ở vòng 1; LLM sai format/tool lỗi/hết ngân sách → `human_review`. Test dùng LLM giả lập tất định; gọi API thật chỉ ở P4.6.
- **Hoàn tất:** trace tool có ID trong `tool_trace_ids`; token/chi phí ghi vào `decisions`.

### P4.5 — Evaluator cho `decisions`
- **Mục tiêu:** chấm `decisions` theo [evaluation.md](evaluation.md) mục 3–4: timely routing recall, unsafe truth rate, dangerous closure rate.
- **Phụ thuộc:** P4.3, P3.4.
- **Kiểm tra:** truth của quyết định = sự cố có `effect_available_at <= decision_at` và giao phạm vi; `close` không bao giờ đạt routing; mẫu số 0 → NA; unmatched không thành benign. Triage KHÔNG import evaluator.
- **Hoàn tất:** bảng kết quả smoke (dù còn xấu) trong thư mục private.

### P4.6 — Đo token trên 3 case
- **Mục tiêu:** chạy agent một bước với LLM thật trên 3 incident cố định, ghi token và chi phí.
- **Phụ thuộc:** P4.4. **Cần người dùng:** API key và trần chi phí tạm (MASTER mục 5).
- **Hoàn tất:** số token thật trong MASTER mục 5; mốc ★ đạt.

## Câu hỏi cần người dùng chốt trước khi code

1. **`decision_at` — đã chốt (người dùng giao planner chọn, 26/10):** vòng 1 đặt `decision_at = as_of_time` của snapshot, không cộng độ trễ xử lý giả định. Độ trễ thật (gồm thời gian gọi LLM) ghi riêng trong cột token/chi phí, không đổi `decision_at`.
2. **LLM — đã chốt (người dùng, 26/10):** Qwen 3.7 Flash qua OpenRouter, đọc `OPENROUTER_API_KEY` từ môi trường/`.env` (không commit, không log key). Chi tiết model và hệ quả (không có JSON schema chặt): [rationale.md](rationale.md) mục 3. Trần chi phí tạm: **1 USD** cho toàn bộ P4 vòng 1 (người dùng chốt 26/10).
3. **Rule vòng 1:** chọn `unit_or_null_change` (smoke có 2 sự cố `data_pipeline` được phát hiện) thay vì `seasonal_calendar` (2 incident seasonal hiện unmatched do hiệu ứng trễ cửa sổ — cần matcher vòng 2 trước).

## Nghiệm thu P4 vòng 1
- [x] Gate đúng từng nhánh của [triage.md](triage.md) mục 2; `evidence_status` do code tính.
- [ ] Một rule + agent một bước chạy end-to-end trên smoke; không tool nào đọc được truth.
- [ ] Evaluator chấm `decisions` theo `decision_at`; bảng kết quả ★.
- [ ] Token thật của 3 case ghi vào MASTER mục 5.
- [ ] Test runtime xanh.
