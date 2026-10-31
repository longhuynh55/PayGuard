# Monitoring — metric, signal, alert và incident

> Đầu vào: giao dịch theo as-of ([data.md](data.md)), `model_scores` ([model.md](model.md)). Đầu ra: bảng `alerts` và `incidents` cho triage.
> Stage xây: P3.

## 1. Bốn tầng: metric → signal → alert → incident

- Job chạy mỗi ngày trên giai đoạn monitoring; PSI dùng cửa sổ 7 ngày, còn metric volume là một ngày theo `configs/monitoring.yaml` `metric_windows`.
- **Metric:** PSI/KS trên feature, phân phối score và uncertainty, tỉ lệ cảnh báo/review của model, **CBPE** (NannyML) ước lượng performance khi chưa có nhãn. Gồm cả feature phía người nhận (tỉ lệ người gửi lần đầu, chuyển ra trong 24 h, số tiền sát 10 triệu, tài khoản mới/ngủ lâu có tiền vào); segment TRANSFER tách riêng. Cửa sổ cash-out 24 h chỉ dùng khi đã quan sát đủ hoặc ghi rõ censored.
- **Reference:** PSI/KS dùng 11/2025–1/2026, không cần nhãn. CBPE fit trên tháng 10 có nhãn theo policy với predictor cố định; CBPE không thay calibrator của model/conformal.
- **P3.1 vòng 1:** cấu hình [monitoring.yaml](../configs/monitoring.yaml) chọn các feature P2.2 để tính PSI và khai báo bin, epsilon, provenance. PSI dùng hai quantile bins, pseudocount một transaction count mỗi bin, và một bin thiếu riêng; giá trị feature NULL sau khi đã quan sát được tính là missing thay vì bị bỏ khỏi phân phối. Tỉ lệ review là số `decision_region = review` chia tổng model score trong cửa sổ 7 ngày; do `model_scores` hiện chỉ lưu episode sau T0, metric này chưa có reference và chưa dùng để sinh signal; phần reference được để sang vòng 2 ([trạng thái bàn giao](p3-implementation.md)).
- **Metric volume P3.7:** `volume.txn_count` đếm giao dịch episode có `event_at` thuộc đúng ngày nghiệp vụ và `available_at <= as_of_time`. Nền là trung vị số đếm mỗi ngày của reference base 11/2025–1/2026, tính cả ngày zero và không chia theo thứ trong tuần; median channel được dùng cho calendar event có scope channel. `value` là độ lệch tương đối quan sát được so với nền. `reference_window` lưu phương pháp `reference_median_1d_count` cùng mức thay đổi tương đối mà public `business_calendar` kỳ vọng. Feature `txn_count` được evaluator ánh xạ tường minh sang truth feature `txn_count` trong `evaluation/matcher.py`. Volume không áp `minimum_sample_count`; PSI mới dùng cổng mẫu này.
- **Signal:** ngưỡng khởi đầu PSI > 0,25, KS p < 0,01; Benjamini–Hochberg làm baseline phụ. **Chỉnh trên episode clean thuộc development**, khóa trước calibration/test. Vòng 1 mới triển khai PSI; KS ở vòng 2. PSI phát alert khi PSI > 0,25. Volume dùng một ngưỡng chung: phát alert khi `abs(value - business_calendar_expected_relative_change)` > ngưỡng, kể cả ngày không có calendar (expected = 0). Ngưỡng volume smoke là 4,25, thống kê chính xác là maximum tuyệt đối của residual một ngày trên 30 snapshot clean development `ep-0001`, sau baseline median. Threshold được khóa trước khi kiểm các episode còn lại; KS p < 0,01 và Benjamini–Hochberg vẫn là baseline phụ chưa triển khai.
- **Chẩn đoán pha tuần và độ nhạy:** cấu hình simulator tạo đợt card-testing mỗi bảy ngày địa phương. Trong reference base, đợt đó rơi vào thứ Ba: trung bình thứ Ba là 13,0 giao dịch/ngày so với 3,1–4,0 ở các thứ khác, và 11 ngày đông giao dịch nhất đều là thứ Ba. T0 của episode rơi vào Chủ nhật; trên clean `ep-0001`, ngày 01/02 có 18/21 giao dịch bằng card, chủ yếu từ customer-000015 (9) và customer-000019 (8). Đây là lệch pha chiến dịch, không phải hiệu ứng thứ trong tuần. Median toàn reference là 4 giao dịch/ngày trên 92 ngày, nên baseline volume không phụ thuộc pha thứ.
- **P3.7 PSI diagnosis/calibration:** cửa sổ PSI đầu có 10/12 quan sát trong bin đầu từ customer-000015, phần lớn là card-testing; nguyên nhân là cụm theo khách hàng với effective n nhỏ. Pseudocount `1e-6` khuếch đại sparse bins (PSI 3,4004 ngày 07/02; 24/24 ngày đủ cửa sổ > 0,25). Dùng 2 thay vì 10 bin giảm clean development còn 1/24 ngày vượt 0,25 trước cổng sample (max 0,307), và clean phát 0 alert; đây là đánh đổi độ nhạy. Với pipeline ×100 kéo dài tới 02/03, PSI 2-bin vượt ngưỡng 7/24 ngày; riêng 15/02 PSI 0,07 so với 12,96 khi dùng 10 bin. Cần đánh giá lại độ nhạy 10→2 bin ở vòng 2 trên world lớn hơn hoặc pilot.
- **P3.7 volume calibration result:** sau baseline median, 30 residual tuyệt đối của clean development `ep-0001` có min 0, median 0,5, max 4,25 (ngày 01/02). Ngày seasonal 05/02 có residual 3,0 ở `ep-0003` và 3,5 ở `ep-0005` so với kỳ vọng 1,25. Do residual seasonal nhỏ hơn nhiễu clean cực đại, không có một ngưỡng duy nhất vừa loại sạch alert nền vừa phát hiện cả hai ngày seasonal. Ngưỡng khóa theo max clean là 4,25; so sánh nghiêm ngặt nên smoke hiện chưa phát volume alert cho hai ngày seasonal. Nghiệm thu seasonal-volume-match **hoãn tới pilot 60 / P1.6 vòng 2** (quyết định người dùng 31/10, [ghi chú P3.7](p3-implementation.md)); không hạ ngưỡng có điều kiện.
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
