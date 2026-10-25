# Orchestration — Claude tự điều phối coder và reviewer

> Hợp đồng chạy tự động. Vai trò và model: [AGENTS.md](../AGENTS.md). Task và nghiệm thu từng stage: sub-plan của stage (P1: [p1-implementation.md](p1-implementation.md)).

## Goal hiện tại (chốt 25/10/2026)

**P1 → P2 → P3**, theo thứ tự hai vòng: vòng 1 cả ba stage tới mốc ★ luồng mỏng ([MASTER_PLAN.md](../MASTER_PLAN.md)), sau đó vòng 2. Chạy liên tục, không dừng giữa các task trừ khi gặp điều kiện dừng bên dưới.

## Vòng lặp mỗi task

```
GIAO (prompt theo sub-plan) → CHỜ (herdr agent wait) → TEST → REVIEW (gpt-6.1-sol)
   → CHANGES_REQUIRED: gửi coder sửa, quay lại TEST (TỐI ĐA 2 vòng review)
   → PASS + test xanh: TỰ MERGE vào main → cập nhật Trạng thái → task kế tiếp
```

1. **Giao:** `herdr worktree create --branch slice/<task> --base main`; khởi động coder; prompt gồm task ID, vòng, luật Implementer, lệnh kiểm tra và dòng kết thúc `TASK <id> ROUND<n> DONE`. CHỈ gửi prompt khi coder `idle`/`done` — gửi lúc `blocked` sẽ mất.
2. **Test:** trong worktree chạy `uv run pytest`, `python3 review/verify_plan_claims.py`, `python3 review/check_links.py`; lưu vào `review/findings/<task>-tests.txt`. Đỏ → gửi coder sửa (không tính là vòng review).
3. **Review:** `scripts/review_task.sh <task> <vòng>`.
4. **Merge** khi verdict `PASS` và test xanh: `git merge --no-ff slice/<task>` trên `main`, chạy lại test trên `main`, xóa worktree. Conflict hoặc test đỏ sau merge → DỪNG.
5. **Ghi nhận:** dòng trạng thái task trong sub-plan + bảng Trạng thái MASTER khi xong stage.

Sub-plan của P2/P3 do Claude viết trước khi stage bắt đầu, cùng format task của P1.

## Ranh giới tự quyết

| Claude TỰ LÀM | Claude DỪNG, báo người dùng (`herdr notification`) |
|---|---|
| Giao task đúng sub-plan; chạy test, review | Câu hỏi thiết kế/semantics mà docs CHƯA trả lời |
| Trả lời coder KHI docs đã có câu trả lời (trích nguồn) | Xin quyền, trust, lệnh nguy hiểm, cài gói ngoài uv |
| Gửi coder sửa finding "chặn MVP", ≤ 2 vòng | Hết 2 vòng vẫn còn finding chặn |
| Tự merge khi PASS + test xanh | Đổi protocol, làm phần Mở rộng, đổi split/ngưỡng đã khóa |
| Viết sub-plan P2/P3 theo docs đã khóa | Merge conflict, test đỏ trên main sau merge |
| Cập nhật trạng thái, nhật ký | Coder kẹt > 45 phút không tiến triển |

## Môi trường của coder

Sandbox `workspace-write` không ghi được `~/.cache/uv` → coder dùng `UV_CACHE_DIR=/tmp/payguard-uv-cache`.

## Nhật ký chạy

| Thời điểm | Task | Kết quả |
|---|---|---|
| 25/10 23:48 | P1.1 r1 | Coder hỏi 3 semantics (timestamp, tiền, lịch sử trước T0) → người dùng chốt: UTC + lịch VN, VND nguyên, lịch sử chung `episode_id = 'base'` |
