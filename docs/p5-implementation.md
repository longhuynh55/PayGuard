# P5 — Sub-plan chuẩn bị đánh giá

> Trạng thái: **người dùng duyệt 30/10/2026** sau phản biện kiến trúc (orchestrator + supervisor). Hợp đồng chấm điểm: [evaluation.md](evaluation.md); schema: [data.md](data.md); triage: [triage.md](triage.md). File này sở hữu thứ tự và nghiệm thu các task chuẩn bị P5; không chép hợp đồng.
> Điều phối: [orchestration.md](orchestration.md). Ngân sách và số episode đã khóa: [MASTER_PLAN.md](../MASTER_PLAN.md) mục 5.

## Vì sao làm trước P1.4

Phản biện 30/10 chỉ ra ba rủi ro chặn câu hỏi nghiên cứu: (1) chưa chứng minh có ca mà điều kiện score UQ đổi được quyết định ([evaluation.md](evaluation.md) mục 6 thừa nhận A ≡ B có thể xảy ra); (2) matcher chưa có hiệu ứng trễ cửa sổ và ca mơ hồ nên đếm support benign sẽ sai; (3) không có harness chạy nhiều policy trên cùng input, và trần 10 USD chưa được thực thi qua nhiều tiến trình. Thứ tự 4 đợt P1 vòng 2 giữ nguyên; các task dưới đây chen trước P1.4.

## Task

| Task | Ai | Phụ thuộc | Nội dung |
|---|---|---|---|
| P5.0a Bảng quyết định UQ | Orchestrator (tài liệu) | — | Liệt kê theo nhóm sự cố trong [data.md](data.md) mục Kịch bản: ca nào tới agent, quyền đóng nào áp dụng, ca nào điều kiện `close_score >= tau_agent` có thể đổi close/routing. Kết luận chấp nhận cả "không thêm giá trị" |
| P5.2 Matcher hoàn chỉnh | Codex | — | Như mục P5.2 dưới |
| P5.0b Bảng support development | Codex | P5.2 | Như mục P5.0b dưới |
| P5.1 Harness + sổ chi phí | Codex | P4.1 vòng 2 merge | Như mục P5.1 dưới |

### P5.2 — Matcher hoàn chỉnh trước calibration
- **Mục tiêu:** matcher đúng [evaluation.md](evaluation.md) mục 2: thời gian gồm hiệu ứng trễ của cửa sổ metric, segment, feature; trạng thái `matched` / `ambiguous_match` / `unmatched`.
- **Nghiệm thu:** fixture phản ví dụ cho (a) incident mở muộn trong cửa sổ trễ vẫn `matched`; (b) hai truth cùng ứng viên → `ambiguous_match`, không chấm accuracy; (c) ca nhiều nguyên nhân; (d) unmatched không thành benign. Matcher khóa trên development; không đọc calibration/test để chỉnh. Hành vi trên smoke hiện có được liệt kê cũ → mới.

### P5.0b — Bảng support trên development
- **Mục tiêu:** đo trên episode `development` (không calibration/test): số truth duy nhất theo episode và nhóm nguyên nhân; matched/ambiguous/unmatched; cửa sổ đủ; rule coverage; số ca tới agent; số ca đủ điều kiện đóng. Báo cáo private.
- **Nghiệm thu:** bảng tái lập bằng một lệnh; ghi rõ là pilot/feasibility, không tăng episode hay đổi 12/24/24.

### P5.1 — Harness tối thiểu và sổ chi phí bền
- **Mục tiêu:** một lệnh chạy dữ liệu → model → monitoring → alert → triage → báo cáo (MASTER mục 1). Base đóng băng sau scores/metrics/alerts, trước mọi incident/decision; mỗi policy chạy trên bản copy operational riêng; incident tính lại theo lịch sử decision riêng.
- **Danh tính run:** manifest gồm hash code/config/prompt/sampling/base; kiểm tra trước resume, lệch → dừng. Sổ seed/role: development được phân tích, calibration chỉ sau khi khóa prompt/rule, test chỉ sau khi khóa policy.
- **Sổ chi phí:** ledger bền cộng mọi lời gọi (mọi nhánh, retry, recheck, lần chạy trước restart); chặn trước khi vượt trần đã khóa.
- **Nghiệm thu:** fingerprint input giống nhau giữa các policy và không đổi sau chạy; resume idempotent; fake provider end-to-end; truth chỉ evaluator đọc; commit trước tool giữ như P4.4b.
