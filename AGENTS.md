# AGENTS.md

PayGuard: fraud monitoring + investigation agent triage alert drift khi nhãn về chậm.
Trả lời người dùng bằng tiếng Việt; code và commit bằng tiếng Anh.

## Bắt đầu
ĐỌC [MASTER_PLAN.md](MASTER_PLAN.md) TRƯỚC: trạng thái, phạm vi MVP, bản đồ `docs/`.

## Tài liệu
- MỖI NỘI DUNG CHỈ Ở MỘT CHỖ. KHÔNG chép giữa các file — link tới chỗ gốc.
- Schema dữ liệu CHỈ định nghĩa trong `docs/data.md`.
- `archive/` CHỈ ĐỌC, KHÔNG sửa, KHÔNG dùng làm căn cứ.
- Số đo ghi "bản 24/10" là của code đã rollback — KHÔNG coi là kết quả.

## Bất biến — KHÔNG ĐƯỢC PHÁ
- **AS-OF:** feature/SQL CHỈ đọc bản ghi có `available_at <= as_of_time` và đúng `episode_id`.
- **TRUTH CÔ LẬP:** truth/injector NẰM NGOÀI mọi DB/file mà tool agent đọc được. CHỈ evaluator đọc truth.
- **PENDING KHÔNG PHẢI NHÃN ÂM.**
- **KHÔNG TUNE TRÊN TEST.** Manifest episode khóa trước tuning.
- `evidence_status` do CODE tính, KHÔNG do LLM tự điền.

## Vai trò
| Vai | Ai | Được sửa |
|---|---|---|
| Planner + orchestrator | **Claude Opus** (Claude Code) | `MASTER_PLAN.md`, `docs/`, task spec |
| Implementer | **Codex `gpt-6-luna`, reasoning max**, `-s workspace-write`, trong worktree của task; task nhỏ/theo khuôn: **Claude Haiku 5.5 max** (người dùng duyệt 28/10) | Code, test, `docs/` khi hợp đồng đổi |
| Reviewer | **Claude Opus 5.5** (sub-agent mới cho mỗi lượt review, do orchestrator gọi; người dùng đổi 27/10, xem [orchestration](docs/orchestration.md)); dự phòng: Codex `gpt-6.1-sol` (supervisor herdr hoặc `scripts/review_task.sh`) | KHÔNG sửa gì trong repo; findings orchestrator chép vào `review/findings/` |

- **Planner/orchestrator:** viết task có tiêu chí nghiệm thu kiểm tra được; giao việc và chờ qua `herdr`; KHÔNG viết code runtime. Agent ở trạng thái `blocked` → BÁO NGƯỜI DÙNG, KHÔNG tự trả lời hay tự duyệt quyền.
- **Implementer:** CHỈ làm MVP; phần *Mở rộng* PHẢI hỏi người dùng. Spec mơ hồ → HỎI trước khi code, KHÔNG tự đoán. KHÔNG sửa/xóa/nới test nghiệm thu hay tiêu chí nghiệm thu để cho qua. KHÔNG XÓA/ĐỔI ngưỡng, giá trị hay quy tắc đã khóa trong `docs/` — hoãn triển khai sang vòng 2 thì CHỈ THÊM ghi chú, không xóa. Code đổi hợp đồng (schema, bảng, ngưỡng) → PHẢI sửa `docs/` TRONG CÙNG THAY ĐỔI. Implementer KHÔNG commit — orchestrator commit sau test. Sửa protocol → cập nhật `review/verify_plan_claims.py` + nhật ký quyết định trong MASTER. Xong việc → cập nhật Trạng thái trong MASTER.
- **Reviewer:** KHÔNG sửa file. Mỗi phát hiện ghi `file:line`, phản ví dụ hoặc lệnh đã chạy, đề xuất sửa, mức (chặn MVP / không chặn). Kiểm tra cả lệch giữa code và `docs/`. Protocol đã khóa — KHÔNG mở vòng research mới. TỐI ĐA 2 VÒNG review mỗi task, sau đó dừng hỏi người dùng.
- **Người dùng chốt.** Bất đồng giữa các agent → trình bày cả hai lập luận, KHÔNG tự quyết.

## Trước khi báo xong — LUÔN chạy
```bash
export UV_CACHE_DIR=/tmp/payguard-uv-cache
uv run python -m simulator generate --profile smoke --out data/p1-smoke
uv run python -m simulator validate --run data/p1-smoke
python3 review/verify_plan_claims.py
python3 review/check_links.py
```
Môi trường dùng **uv** (Python 3.12); trong sandbox Codex đặt `UV_CACHE_DIR=/tmp/payguard-uv-cache`. Luật tự điều phối và điều kiện DỪNG: [docs/orchestration.md](docs/orchestration.md). Khi có code: chạy test bằng `uv run pytest`; KHÔNG `pip install` ngoài uv.

## Git
CHỈ commit/push khi người dùng yêu cầu. KHÔNG đưa `data/`, `.venv/`, cache vào git.
