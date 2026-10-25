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
- **Implementer:** CHỈ làm MVP; phần *Mở rộng* PHẢI hỏi người dùng. Sửa protocol → cập nhật `review/verify_plan_claims.py` + nhật ký quyết định trong MASTER. Xong việc → cập nhật Trạng thái trong MASTER.
- **Reviewer:** KHÔNG sửa file. Mỗi phát hiện ghi `file:line`, phản ví dụ hoặc lệnh đã chạy, đề xuất sửa, mức (chặn MVP / không chặn). Protocol đã khóa — KHÔNG mở vòng research mới.
- **Người dùng chốt.** Bất đồng giữa các agent → trình bày cả hai lập luận, KHÔNG tự quyết.

## Trước khi báo xong — LUÔN chạy
```bash
python3 review/verify_plan_claims.py
python3 review/check_links.py
```

## Git
CHỈ commit/push khi người dùng yêu cầu. KHÔNG đưa `data/`, `.venv/`, cache vào git.
