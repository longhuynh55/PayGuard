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

1. **Giao:** `herdr worktree create --branch slice/<task> --base main`; khởi động coder bằng `codex -m gpt-6-luna -c 'model_reasoning_effort="max"' -s workspace-write -a never -c 'sandbox_workspace_write.network_access=true'` (không bao giờ hỏi; lệnh bị sandbox chặn trả lỗi về cho model; có mạng cho uv; CHỈ ghi trong worktree); prompt gồm task ID, vòng, luật Implementer, lệnh kiểm tra và dòng kết thúc `TASK <id> ROUND<n> DONE`. CHỈ gửi prompt khi coder `idle`/`done` — gửi lúc `blocked` sẽ mất.
   Coder **KHÔNG commit** (thư mục `.git` của worktree nằm ngoài sandbox); để nguyên thay đổi.
2. **Test:** trong worktree chạy `uv run pytest`, `python3 review/verify_plan_claims.py`, `python3 review/check_links.py`; lưu vào `review/findings/<task>-tests.txt`. Đỏ → gửi coder sửa (không tính là vòng review).
3. **Commit + review:** orchestrator commit thay đổi trong worktree, rồi `scripts/review_task.sh <task> <vòng>`. Sửa sau review → commit mới trên cùng nhánh.
4. **Merge** khi verdict `PASS` và test xanh: `git merge --no-ff slice/<task>` trên `main`, chạy lại test trên `main`, xóa worktree. Conflict hoặc test đỏ sau merge → DỪNG.
5. **Ghi nhận:** dòng trạng thái task trong sub-plan + bảng Trạng thái MASTER khi xong stage.

Sub-plan của P2/P3 do Claude viết trước khi stage bắt đầu, cùng format task của P1.

## Ranh giới tự quyết

| Claude TỰ LÀM | Claude DỪNG, báo người dùng (`herdr notification`) |
|---|---|
| Giao task đúng sub-plan; chạy test, review | Câu hỏi thiết kế/semantics mà docs CHƯA trả lời **và** đụng protocol đã khóa |
| Trả lời coder KHI docs đã có câu trả lời (trích nguồn); docs chưa có và KHÔNG đụng protocol → chọn phương án THẬN TRỌNG NHẤT, ghi vào "Quyết định chờ duyệt" | Xin quyền, trust, lệnh nguy hiểm, cài gói ngoài uv |
| Gửi coder sửa finding "chặn MVP", ≤ 2 vòng | Hết 2 vòng vẫn còn finding chặn |
| Tự merge khi PASS + test xanh | Đổi protocol, làm phần Mở rộng, đổi split/ngưỡng đã khóa |
| Viết sub-plan P2/P3 theo docs đã khóa; gộp conflict CHỈ ở bảng trạng thái/nhật ký của MASTER và sub-plan (giữ cả hai phía, ghi nhật ký) | Merge conflict ở code, test hoặc nội dung hợp đồng `docs/`; test đỏ trên main sau merge |
| Cập nhật trạng thái, nhật ký | Coder kẹt > 45 phút không tiến triển |

## Quyết định chờ duyệt

Quyết định Claude tự chọn khi chạy không người (người dùng đã cho phép 26/10). Người dùng duyệt lại; không đồng ý → sửa ở task sau.

| Thời điểm | Task | Câu hỏi | Đã chọn | Lý do |
|---|---|---|---|---|
| 26/10 09:00 | P2.3 | Ngưỡng support để dùng isotonic (dưới ngưỡng → sigmoid); `model.md` chỉ ghi "sigmoid nếu ít support" | **≥ 25 mẫu mỗi lớp** (không phải ≥ 100 mẫu tổng như coder đề xuất); smoke 24 fraud → sigmoid; test cả hai nhánh | Isotonic overfit khi lớp hiếm ít mẫu — điều kiện theo lớp thận trọng hơn tổng. Giả định chưa tune, đổi được ở vòng 2 |
| 26/10 08:25 | P2.1 | Chạy song song để nhanh hơn? | **Người dùng duyệt:** 2 luồng — A: P1.6→P1.7→P1.8; B: P2.1→P2.2 trên main hiện tại. P2.1 dựng dữ liệu smoke bằng API simulator (`read_as_of`, snapshot nhãn), không đọc file artifact; luồng B KHÔNG sửa `simulator/`, `configs/simulator.yaml`, `docs/data.md` | Split fit/validation/calibration đều trước T0, P1.6–P1.8 không đổi lịch sử nền. Khi P1.8 xong, P2.1 vòng 2 chuyển sang đọc artifact CLI nếu format khác |
| 26/10 03:00 | P1.4 | P1.3 vòng 1 chỉ có kênh QR, nhưng card testing là fraud trên thẻ | Thêm mã kênh `card` trong config cho giao dịch card testing; không đổi schema (cột `channel` đã có) | Thận trọng nhất: không đụng schema; kênh thẻ đầy đủ cho giao dịch hợp lệ vẫn để vòng 2 P1.3 |
| 26/10 00:25 | P1.2 | Trạng thái vòng đời giao dịch (authorized/settled…) lộ tương lai nếu nằm trên cùng một hàng | Bảng append-only `transaction_events`, mỗi sự kiện có `available_at` riêng | Khớp mẫu label events append-only; hoãn cả hàng tới settlement sẽ ẩn giao dịch đang xử lý khỏi monitoring |
| 26/10 00:17 | P1.2 | Engine lưu trữ: SQLite hay DuckDB (`docs/overview.md` ghi DuckDB) | Coder chọn **SQLite** (thư viện chuẩn) cho storage P1 | Không cần dependency; DuckDB đọc trực tiếp được file SQLite nên P2 vẫn có thể tính feature bằng DuckDB. Nếu muốn thống nhất DuckDB ngay → đổi ở vòng 2 P1.2 |

## Môi trường của coder

Sandbox `workspace-write` không ghi được `~/.cache/uv` → coder dùng `UV_CACHE_DIR=/tmp/payguard-uv-cache`.

## Nhật ký chạy

| Thời điểm | Task | Kết quả |
|---|---|---|
| 25/10 23:48 | P1.1 r1 | Coder hỏi 3 semantics (timestamp, tiền, lịch sử trước T0) → người dùng chốt: UTC + lịch VN, VND nguyên, lịch sử chung `episode_id = 'base'` |
| 26/10 00:01 | P1.1 r1 | Coder bị chặn ở `git add` (.git ngoài sandbox) → đổi quy trình: orchestrator commit. 10 test pass; commit `1fd078b`; review vòng 1 đang chạy |
| 26/10 00:10 | P1.1 | Review vòng 1 CHANGES_REQUIRED (3 finding chặn: H, timezone, VND) → coder sửa, 17 test → review vòng 2 PASS → merge. Conflict ở bảng trạng thái MASTER (status P1 vs link sub-plan P2/P3) → gộp tay giữ cả hai; luật conflict được làm rõ |
| 26/10 00:12 | P1.2 | Codex khởi động lỗi `workspace routing discovery timed out` (log daemon: request tới OpenAI timeout lặp lại) → thử lại, lần 1 OK. Coder xong: 30 test; commit `49ed6ae`; review vòng 1 đang chạy |
| 26/10 00:25 | P1.2 | Review vòng 1 CHANGES_REQUIRED: 4 finding chặn (lộ trạng thái tương lai, PK nhận NULL, UPDATE xuyên episode, amount NOT NULL chặn kịch bản NULL) → coder đang sửa |
| 26/10 00:32 | P1.2 | Coder sửa 4 finding (36 test, commit `55451a7`). Review vòng 2 CHANGES_REQUIRED: 1 finding chặn mới — event vòng đời của giao dịch base qua ranh giới T0 bị ẩn vĩnh viễn. **DỪNG: hết 2 vòng, chờ người dùng.** |
| 26/10 | P1.2 | Người dùng duyệt vòng sửa 3 (ngoại lệ) cho finding qua ranh giới T0: `transaction_events.episode_id` riêng, event ≥ T0 ghi theo episode. Script review cho vòng 3 chỉ khi `ALLOW_EXTRA_ROUND=1` |
| 26/10 01:08 | P1.2 | Vòng sửa 3 → 38 test → review vòng 3 PASS → merge `0487c3a`. Giao P1.3 vòng 1 |
| 26/10 02:55 | P1.3 r1 | Coder xong (46 test). Review lần đầu lỗi 3 lần do mạng tới OpenAI → chạy lại → PASS, 1 finding không chặn (Poisson sampler sai ở mean lớn/rất nhỏ → backlog vòng 2) → merge. Giao P1.4 vòng 1 |
| 26/10 03:25 | P1.4 r1 | Coder xong (50 test). Review vòng 1 CHANGES_REQUIRED: card testing dồn vào T0−1 nên fit/validation/calibration không có fraud; bộ đếm benign sai → coder sửa (51 test, chiến dịch 12/2/4 theo cohort) → review vòng 2 PASS → merge. Giao P1.5 |
| 26/10 08:10 | P1.5 r1 | Coder xong 03:28 (57 test, commit `8e3f09f`). Review treo ~4h45 ở `Reading additional input from stdin` (codex exec chạy nền không đóng stdin) → kill, sửa `review_task.sh` thêm `< /dev/null`, chạy lại có `timeout 2400` |
| 26/10 08:15 | P1.5 r1 | Review vòng 1 PASS, không finding → merge (57 test trên main). Giao P1.6 vòng 1 |
| 26/10 08:25 | P2.1 r1 | Mở luồng B song song (người dùng duyệt) |
| 26/10 08:45 | P1.6 + P2.1 r1 | Cả hai review vòng 1 PASS, không finding → merge P1.6 rồi P2.1; conflict CHỈ ở bảng Trạng thái MASTER → gộp giữ cả hai. 64 test trên main. Sửa `review_task.sh` chọn sub-plan theo stage. Giao P1.7 (A) và P2.2 (B) |
| 26/10 08:55 | P2.2 r1 | Review vòng 1 CHANGES_REQUIRED: coder xóa tiêu chí nghiệm thu "test as-of trên fixture và smoke" trong sub-plan (cấm nới nghiệm thu) → khôi phục + thêm test smoke P2.1→P2.2 (69 test) → review vòng 2 PASS → merge. Giao P2.3 (B); P1.7 đang review |
| 26/10 09:05 | P2.3 r1 | Coder hỏi ngưỡng support isotonic → orchestrator chọn ≥ 25/lớp (xem Quyết định chờ duyệt). 74 test → review vòng 1 PASS → merge. P1.7 review vòng 1 CHANGES_REQUIRED (episode_id lộ template; label report lỗi theo episode) → đang review vòng 2. Giao P2.4 (B) |
| 26/10 09:15 | P1.7 r2 | Sửa: episode ID trung tính `ep-0001…`, label report theo episode (68 test) → review vòng 2 PASS → merge; conflict CHỈ ở dòng Trạng thái/Việc tiếp theo MASTER → gộp. 78 test trên main. Giao P1.8 (A) |
| 26/10 09:25 | P2.4 r2 | Review vòng 1 CHANGES_REQUIRED: coder thay câu protocol khóa "thiếu support → mở rộng lịch sử/giảm scope" (một phần do prompt orchestrator) + thiếu số bàn giao → khôi phục nguyên văn, ngưỡng vô hạn chỉ là cờ `insufficient_support` → review vòng 2 PASS → merge; conflict CHỈ ở Trạng thái MASTER. 85 test trên main. Giao P2.5 (B); schema `model_scores` thêm thành mục 6 cuối `data.md` để tránh đụng P1.8 |
| 26/10 09:45 | P1.8 | Review vòng 1 CHANGES_REQUIRED (validate bỏ qua incident_truth; tập 0 positive vẫn PASSED) → sửa (86 test) → review vòng 2 CHANGES_REQUIRED: validate chưa đối chiếu `effect_available_at` với bản ghi bị tác động (dịch mọi mốc +365 ngày vẫn PASSED). **DỪNG: hết 2 vòng, chờ người dùng.** |
| 26/10 09:55 | P2.5 | Review vòng 1 CHANGES_REQUIRED (report bỏ cờ `insufficient_support`) → sửa (87 test) → review vòng 2 PASS, 1 finding không chặn (`training_audit.json` thiếu `model_version` → backlog vòng 2) → merge. **P2 vòng 1 xong.** Giao P3.1 (B) trên main: chỉ cần P2.5 + dữ liệu P1, không chờ CLI P1.8 |
