# Monitoring — metric, signal, alert và incident

> Đầu vào: giao dịch theo as-of ([data.md](data.md)), `model_scores` ([model.md](model.md)). Đầu ra: bảng `alerts` và `incidents` cho triage.
> Stage xây: P3.

## 1. Bốn tầng: metric → signal → alert → incident

- Job chạy mỗi ngày trên giai đoạn monitoring; cửa sổ 7 ngày so với reference.
- **Metric:** PSI/KS trên feature, phân phối score và uncertainty, tỉ lệ cảnh báo/review của model, **CBPE** (NannyML) ước lượng performance khi chưa có nhãn. Gồm cả feature phía người nhận (tỉ lệ người gửi lần đầu, chuyển ra trong 24 h, số tiền sát 10 triệu, tài khoản mới/ngủ lâu có tiền vào); segment TRANSFER tách riêng. Cửa sổ cash-out 24 h chỉ dùng khi đã quan sát đủ hoặc ghi rõ censored.
- **Reference:** PSI/KS dùng 11/2025–1/2026, không cần nhãn. CBPE fit trên tháng 10 có nhãn theo policy với predictor cố định; CBPE không thay calibrator của model/conformal.
- **P3.1 vòng 1:** cấu hình [monitoring.yaml](../configs/monitoring.yaml) chọn các feature P2.2 để tính PSI và khai báo bin, epsilon, provenance. PSI dùng hai quantile bins, pseudocount một transaction count mỗi bin, và một bin thiếu riêng; giá trị feature NULL sau khi đã quan sát được tính là missing thay vì bị bỏ khỏi phân phối. Tỉ lệ review là số `decision_region = review` chia tổng model score trong cửa sổ 7 ngày; do `model_scores` hiện chỉ lưu episode sau T0, metric này chưa có reference và chưa dùng để sinh signal; phần reference được để sang vòng 2 ([trạng thái bàn giao](p3-implementation.md)).
- **Metric volume P3.7:** `volume.txn_count` đếm số giao dịch của đúng ngày nghiệp vụ. Nền là trung bình số giao dịch base theo cùng thứ trong tuần của reference 11/2025–1/2026, tính cả ngày có zero giao dịch. `value` là độ lệch tương đối quan sát được so với nền; `reference` mang thêm mức thay đổi tương đối mà public `business_calendar` kỳ vọng, tổng hợp theo nền channel có phạm vi. Feature là `txn_count`, nên trùng tên feature truth mà không cần sửa mapping trong evaluator. Tín hiệu có ngưỡng relative change 0,50 khi có kỳ vọng calendar và 6,0 cho thay đổi không khai báo trên lịch.
- **Signal:** P3.7 dùng PSI > 0,25 với điều kiện nghiêm ngặt; volume so trị tuyệt đối độ lệch quan sát và độ lệch so với kỳ vọng calendar. Threshold được hiệu chỉnh trên clean development, khóa trước calibration/test; ngưỡng KS p < 0,01 và Benjamini–Hochberg vẫn là baseline phụ chưa triển khai.
- **P3.7 calibration:** hiện profile smoke cung cấp một clean development episode (`ep-0001`), không có profile pilot runtime để dùng thêm clean development episodes. Reference có 359 observation; mỗi cửa sổ `amount_avg_7d` có 17–36 observation, trong đó ngày đầu có histogram reference `[36,36,36,36,35,36,36,36,36,36]` và cửa sổ `[0,12,6,2,2,4,3,4,0,3]` theo mười quantile bins. Pseudocount `1e-6` làm bin rỗng chi phối PSI (3,4004 ngày 07/02; 24/24 ngày đủ cửa sổ > 0,25). Sau khi chuyển sang hai bin, pseudocount một count và bin missing riêng, max PSI của 30 snapshot là 0,4110; riêng 24 snapshot đủ cửa sổ có max 0,3069, với 1/24 ngày vượt 0,25 trước cổng sample. Ngày đó bị chặn bởi `small_window_sample`; clean development phát 0 alert. Volume lệch tuần sạch tối đa 0,388 và lệch ngày không có calendar tối đa 5,825; ngưỡng volume 0,50 / 6,0 lần lượt ở trên các cực đại development tương ứng. Không dùng calibration/test để chọn các giá trị này.
- Khi nhãn về (trễ): tính performance thật → so với ước lượng CBPE. CBPE không báo động **không** đủ để kết luận benign.

## 2. Gộp alert thành incident

- Alert mới trùng feature/segment, cách ≤ 2 ngày với một incident **đang mở** (chưa có quyết định cuối, hoặc đang recheck) → gộp vào incident đó.
- Incident đã có quyết định cuối (close / data_ticket / fraud_ops / human_review) mà có alert mới → tạo **incident con** có `parent_incident_id`. Không mở lại, không sửa quyết định cũ.
- Làm rõ mốc thời gian: với incident đang mở, khoảng cách ≤ 2 ngày được tính từ alert gần nhất đã gộp vào incident.
- Làm rõ incident con: alert cùng feature/segment sau quyết định cuối chỉ thành incident con nếu cách alert gần nhất của incident cha ≤ 2 ngày; xa hơn thì tạo incident mới độc lập với `parent_incident_id = NULL`.
- Chấm theo incident, báo cáo kèm số alert. Cách chấm từng quyết định theo thời điểm: [evaluation.md](evaluation.md).
- Số incident con có thể khác giữa các policy vì thời điểm quyết định cuối khác nhau. Giữ nguyên alert và truth đầu vào khi so policy; báo số case con như workload. Metric rủi ro so sánh chính dùng tập truth/episode cố định theo [evaluation.md](evaluation.md), không dùng số incident con làm mẫu số chính.

| Bảng | Cột chính |
|---|---|
| `alerts` | `alert_id`, `episode_id`, `as_of_time`, metric, feature, segment, giá trị, reference, ngưỡng; chi tiết schema tại [data.md, mục 7](data.md) |
| `incidents` | `incident_id`, `parent_incident_id`, `episode_id`, danh sách `alert_id`, feature/segment, `opened_at`, trạng thái |

Schema chi tiết của `incidents`, gồm các cột thời gian `event_at`/`available_at`: [data.md, mục 7](data.md).

## 3. Thí nghiệm riêng của monitoring

Với từng kiểu drift (covariate shift / concept drift / fraud mới / lỗi pipeline / mùa vụ): tín hiệu nào báo sớm và báo đúng? Chấp nhận kết quả âm tính (ví dụ concept drift mà model vẫn tự tin). Baseline: chỉ theo dõi nhãn (trễ) · PSI/KS thuần · CBPE.

## 4. Xong khi

Mỗi episode sinh alert (hoặc không) tái lập được; incident con hoạt động đúng; detection recall đối soát được trên toàn bộ sự cố. Dashboard Streamlit thô.
