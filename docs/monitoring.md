# Monitoring — metric, signal, alert và incident

> Đầu vào: giao dịch theo as-of ([data.md](data.md)), `model_scores` ([model.md](model.md)). Đầu ra: bảng `alerts` và `incidents` cho triage.
> Stage xây: P3.

## 1. Bốn tầng: metric → signal → alert → incident

- Job chạy mỗi ngày trên giai đoạn monitoring; cửa sổ 7 ngày so với reference.
- **Metric:** PSI/KS trên feature, phân phối score và uncertainty, tỉ lệ cảnh báo/review của model, **CBPE** (NannyML) ước lượng performance khi chưa có nhãn. Gồm cả feature phía người nhận (tỉ lệ người gửi lần đầu, chuyển ra trong 24 h, số tiền sát 10 triệu, tài khoản mới/ngủ lâu có tiền vào); segment TRANSFER tách riêng. Cửa sổ cash-out 24 h chỉ dùng khi đã quan sát đủ hoặc ghi rõ censored.
- **Reference:** PSI/KS dùng 11/2025–1/2026, không cần nhãn. CBPE fit trên tháng 10 có nhãn theo policy với predictor cố định; CBPE không thay calibrator của model/conformal.
- **P3.1 vòng 1:** cấu hình [monitoring.yaml](../configs/monitoring.yaml) chọn các feature P2.2 để tính PSI và khai báo bin, epsilon, provenance. Tỉ lệ review là số `decision_region = review` chia tổng model score trong cửa sổ 7 ngày; do `model_scores` hiện chỉ lưu episode sau T0, metric này chưa có reference và chưa dùng để sinh signal; phần reference được để sang vòng 2 ([trạng thái bàn giao](p3-implementation.md)).
- **Signal:** P3.2 vòng 1 chỉ dùng PSI > 0,25; ngưỡng lấy nguyên theo monitoring.md, chưa chỉnh. Không dùng KS p-value hay Benjamini–Hochberg trong vòng này. KS, Benjamini–Hochberg và chỉnh ngưỡng trên clean development thuộc vòng 2, khóa trước calibration/test.
- Khi nhãn về (trễ): tính performance thật → so với ước lượng CBPE. CBPE không báo động **không** đủ để kết luận benign.

## 2. Gộp alert thành incident

- Alert mới trùng feature/segment, cách ≤ 2 ngày với một incident **đang mở** (chưa có quyết định cuối, hoặc đang recheck) → gộp vào incident đó.
- Incident đã có quyết định cuối (close / data_ticket / fraud_ops / human_review) mà có alert mới → tạo **incident con** có `parent_incident_id`. Không mở lại, không sửa quyết định cũ.
- Chấm theo incident, báo cáo kèm số alert. Cách chấm từng quyết định theo thời điểm: [evaluation.md](evaluation.md).
- Số incident con có thể khác giữa các policy vì thời điểm quyết định cuối khác nhau. Giữ nguyên alert và truth đầu vào khi so policy; báo số case con như workload. Metric rủi ro so sánh chính dùng tập truth/episode cố định theo [evaluation.md](evaluation.md), không dùng số incident con làm mẫu số chính.

| Bảng | Cột chính |
|---|---|
| `alerts` | `alert_id`, `episode_id`, `as_of_time`, metric, feature, segment, giá trị, reference, ngưỡng; chi tiết schema tại [data.md, mục 7](data.md) |
| `incidents` | `incident_id`, `parent_incident_id`, `episode_id`, danh sách `alert_id`, feature/segment, `opened_at`, trạng thái |

## 3. Thí nghiệm riêng của monitoring

Với từng kiểu drift (covariate shift / concept drift / fraud mới / lỗi pipeline / mùa vụ): tín hiệu nào báo sớm và báo đúng? Chấp nhận kết quả âm tính (ví dụ concept drift mà model vẫn tự tin). Baseline: chỉ theo dõi nhãn (trễ) · PSI/KS thuần · CBPE.

## 4. Xong khi

Mỗi episode sinh alert (hoặc không) tái lập được; incident con hoạt động đúng; detection recall đối soát được trên toàn bộ sự cố. Dashboard Streamlit thô.
