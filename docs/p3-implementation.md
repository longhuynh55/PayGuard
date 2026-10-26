# P3 — Sub-plan triển khai monitoring

> Trạng thái: **P3.1 round1-done (Vòng 1 đã bàn giao); P3.2–P3.4 chưa triển khai**. Hợp đồng: [monitoring.md](monitoring.md); đối soát recall: [evaluation.md](evaluation.md). File này sở hữu thứ tự task và nghiệm thu P3; không chép hợp đồng.
> Format task và luật hai vòng giống [p1-implementation.md](p1-implementation.md). Điều phối: [orchestration.md](orchestration.md).

## Thứ tự hai vòng

| Task | Vòng 1 — luồng mỏng | Vòng 2 |
|---|---|---|
| P3.1 Job hằng ngày + metric | Một nhóm metric: PSI trên vài feature + tỉ lệ review của model | KS, phân phối score/uncertainty, feature phía người nhận, CBPE |
| P3.2 Signal + alert | Ngưỡng khởi đầu (PSI > 0,25) | Benjamini–Hochberg; chỉnh ngưỡng trên clean development |
| P3.3 Incident + incident con | Đầy đủ | — |
| P3.4 Ghép truth + detection recall | Matcher tối thiểu (thời gian + segment + feature) | `ambiguous_match`, hiệu ứng trễ cửa sổ đầy đủ |

## Bố trí file dự kiến

```text
monitoring/   __init__.py, metrics.py, signals.py, incidents.py, __main__.py
evaluation/   __init__.py, matcher.py, recall.py          # evaluator: module duy nhất đọc truth
tests/        test_monitoring_metrics.py, test_monitoring_incidents.py, test_evaluation_recall.py
```

## Các task

### P3.1 — Job hằng ngày và metric (`round1-done` — Vòng 1 đã bàn giao)
- **Mục tiêu:** với mỗi episode, mỗi ngày nghiệp vụ (giờ VN) tính metric cửa sổ 7 ngày so với reference 11/2025–1/2026.
- **Phụ thuộc:** P2.5 (`model_scores`), artifact P1.
- **Kiểm tra:** metric tại ngày d chỉ dùng dữ liệu `available_at <= as_of_time` của d; cùng input → cùng output.
- **Hoàn tất:** bảng metric theo ngày cho smoke.
- **Vòng 1 đã bàn giao:** `model.review_rate` hiện chỉ là tỷ lệ thô vì `model_scores` chưa được chấm trên giai đoạn reference; metric này chưa dùng để sinh signal và phần reference được để sang vòng 2.

### P3.2 — Signal và alert (`todo`)
- **Mục tiêu:** chuyển metric vượt ngưỡng thành `alerts` theo schema [monitoring.md](monitoring.md).
- **Phụ thuộc:** P3.1.
- **Kiểm tra:** episode clean smoke không bùng alert hàng loạt; kịch bản đổi đơn vị P1 sinh alert trên feature bị tác động. Ngưỡng KHÔNG tune trên episode test.
- **Hoàn tất:** bảng `alerts` tái lập được.

### P3.3 — Incident và incident con (`todo`)
- **Mục tiêu:** gộp alert vào incident đang mở; incident đã có quyết định cuối → incident con `parent_incident_id`.
- **Phụ thuộc:** P3.2.
- **Kiểm tra:** gộp trong ≤ 2 ngày cùng feature/segment; không gộp khác segment; incident đã đóng không bị mở lại. Ở P3 chưa có triage: test dùng quyết định giả lập.
- **Hoàn tất:** bảng `incidents` + test.

### P3.4 — Ghép truth và detection recall (`todo`)
- **Mục tiêu:** evaluator ghép incident với bảng truth, tính detection recall trên mọi sự cố cần can thiệp.
- **Phụ thuộc:** P3.3, truth của P1.
- **Kiểm tra:** `unmatched` không tự thành benign; sự cố không có alert tính là bỏ sót; mẫu số 0 → NA. Monitoring/model KHÔNG import module evaluator.
- **Hoàn tất:** báo cáo detection recall theo kịch bản cho smoke; cập nhật Trạng thái MASTER. Vòng 1 xong P3 = đủ đầu vào cho mốc ★ (rule + gate + agent một bước thuộc P4).

## Nghiệm thu P3
- [ ] Metric/alert/incident đúng as-of và tái lập được.
- [ ] Incident con hoạt động đúng; không mở lại incident đã quyết định.
- [ ] Evaluator là nơi duy nhất đọc truth; detection recall đối soát được.
- [ ] Test runtime xanh.
