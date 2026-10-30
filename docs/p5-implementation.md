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
| P3.7 Tính hợp lệ monitoring ([sub-plan P3](p3-implementation.md)) | Codex | P5.2 | Metric volume theo ngày + sửa alert nền `amount_avg_7d` (người dùng duyệt 31/10) |
| P5.0b Bảng support development | Codex | P5.2, P3.7 | Như mục P5.0b dưới |
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

### P5.1 round one — CLI, lock và ngân sách (quyết định người dùng 31/10/2026)

Entrypoint được chốt:

```bash
python -m payguard run --profile smoke --policy rule_human --out data/p5-smoke
python -m payguard run --profile smoke --policy rule_agent_fake --out data/p5-smoke
```

Lệnh thứ hai tái dùng BASE đã freeze; mỗi policy có manifest và operational DB copy riêng. Evaluator report của mỗi policy được giữ dưới `base/private/reports/p5_1/<policy>/`, tránh đặt truth-derived report trong đường dẫn agent/tool. Mặc định chỉ evaluator report trên `development`. Muốn phân tích `calibration` hoặc `test`, thêm `--analysis-role calibration|test`; role ledger ghi lại tập episode và usage. `triage measure` vẫn là mode riêng.

Khóa trước khi phân tích held-out role bằng:

```bash
python -m payguard lock prompt-rule --out data/p5-smoke
python -m payguard lock policy --policy rule_agent_fake --out data/p5-smoke
```

`prompt-rule` là lock dùng chung; `policy` được khóa riêng theo policy. Cả hai lưu thời điểm UTC và hash hiện tại của Git HEAD/dirty flag, code, configs, prompt sources, sampling; policy lock còn gắn `policy_version` và `model_slug`. Lệnh lock từ chối khi Git working tree có thay đổi chưa commit; file Git-ignore không được Git tính là thay đổi. Marker không bị ghi đè: cùng identity thì lệnh idempotent, identity mới cần benchmark root mới. Calibration cần hash khớp prompt/rule lock; test cần hash và policy khớp policy lock. Guard chạy trước evaluator đọc truth.

Cost ledger là `cost-ledger.jsonl` chung dưới benchmark root, dùng append-only reservations/settlements cùng file lock; reservation chưa settle được tính vào ngân sách sau crash. LLM client reserve ước lượng trần input/output theo payload và sampling trước từng lần gọi, gồm retry; khi còn lại không đủ thì không gửi request. Fake provider thêm event chi phí 0. `configs/llm.yaml` giữ nguyên trần tạm $1 cho các lệnh P4; harness truyền trần benchmark $10 vào ledger và LLM client. Hai trần có phạm vi riêng, không cộng thành $11: P4 measurement/run dùng config $1, toàn bộ các policy/retry/recheck dưới cùng benchmark root dùng chung $10. File artifact và identity contract ở [data.md, mục 9](data.md#9-p51-harness-artifacts).
