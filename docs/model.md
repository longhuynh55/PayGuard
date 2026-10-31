# Model — fraud model, calibration và conformal giao dịch

> Đầu vào: giao dịch và snapshot nhãn theo [data.md](data.md). Đầu ra: `model_scores` (score thô, xác suất đã calibration, tập conformal) cho monitoring và triage.
> Stage xây: P2.

## 1. Chia dữ liệu (3 tập)

Mọi cohort có nhãn đã ≥ H = 90 ngày tại T0 = 1/2/2026 (tập trẻ nhất ≥ 92 ngày; kiểm tra trong `review/verify_plan_claims.py`).

| Vai trò | Khoảng giao dịch (đầu gồm, cuối không gồm) |
|---|---|
| Fit model | [01/07/2025, 22/09/2025) |
| Validation (early stopping, chọn model/threshold giao dịch) | [22/09/2025, 01/10/2025) |
| Calibration: fit isotonic + ngưỡng conformal trên score thô + reference CBPE | [01/10/2025, 01/11/2025) |
| Reference drift PSI/KS (không cần nhãn) | [01/11/2025, 01/02/2026) |
| Monitoring/test | 30 ngày sau T0, theo episode |

- **Vì sao gộp isotonic và conformal vào cùng tháng 10:** conformal Mondrian tính trên **score thô** của predictor đã cố định trước tháng 10, nên coverage không phụ thuộc calibrator; isotonic là biến đổi đơn điệu, chỉ để ra xác suất dễ đọc. CBPE tự calibration lại trên reference.
- **Hệ quả:** reliability diagram/ECE **không** tính trên tháng 10 (in-sample của calibrator) mà trên dữ liệu sau T0 khi nhãn về, hoặc truth ở evaluator.
- Audit số lượng và số positive từng tập trước khi chạy. Thiếu support cho calibration theo lớp → mở rộng lịch sử về trước hoặc giảm scope.
- Khi viết code phải giữ đúng đường dữ liệu này: conformal đọc score thô, không đọc output của isotonic.

## 2. Model

- LightGBM + xử lý mất cân bằng; huấn luyện tất định để tái lập được.
- Feature đúng as-of (SQL cửa sổ thời gian trên DuckDB): tần suất theo cửa sổ, số tiền so với thói quen, rủi ro terminal/merchant, feature phía người nhận ([data.md](data.md)).
- **P2.2 vòng 1:** ma trận fit/validation/calibration dùng `txn_count_1h`, `txn_count_24h`, `txn_count_7d`, `amount_sum_7d`, `amount_avg_7d`, và `amount_to_customer_historical_avg`. Cửa sổ là `[created_at − độ_dài, created_at)`; giao dịch mục tiêu bị loại khỏi lịch sử. Tỷ lệ amount dùng trung bình các amount khác NULL của customer trước `created_at`, và là NULL nếu thiếu amount hiện tại, thiếu lịch sử, hoặc trung bình bằng 0.
- Cờ `history_window_observed_{1h,24h,7d}` bằng 1 khi `max(customer.event_at, customer.available_at)` không muộn hơn đầu cửa sổ. Các tổng hợp vẫn phản ánh phần lịch sử quan sát được; cờ 0 đánh dấu phần đó bị censored. Mọi nguồn operational đi qua `read_as_of`; SQL DuckDB còn áp `available_at <= created_at` riêng cho từng giao dịch.
- Target chính là nhãn theo policy; pending không dùng làm 0. *Mở rộng:* oracle-label ablation; khi có `disputed`, loại khỏi supervised fit và báo mức độ loại mẫu.
- Calibration xác suất: isotonic (sigmoid nếu ít support) trên tháng 10, giữ nguyên tỉ lệ lớp của cohort. Reliability diagram trên observed-label và oracle truth ghi riêng.

## 3. Conformal giao dịch

- Ba vùng: singleton legit / singleton fraud / review; tập rỗng và đa nhãn đều review.
- Mondrian alpha theo lớp là cấu hình thực nghiệm; giá trị khởi đầu α_legit = 0,005 và α_fraud = 0,20, theo giả định bản 24/10 cần đo lại trên split mới, chưa tune.
- Với score thô `s = P(fraud)`, nonconformity là `s` cho lớp legit và `1 − s` cho lớp fraud. Ngưỡng lớp là order statistic hạng `ceil((n + 1)(1 − α))` trên các mẫu calibration đúng lớp. Nếu hạng vượt `n`, phép tính dùng ngưỡng vô hạn và đưa lớp đó vào tập dự đoán; report phải đánh dấu `insufficient_support` theo lớp. Đây là tín hiệu áp dụng chính sách ở mục 1 trước khi dùng kết quả, không phải kết quả hợp lệ để bàn giao.
- Coverage theo lớp là số mẫu mà nhãn thật nằm trong tập dự đoán chia số mẫu thật của lớp đó. False approval là fraud được quyết định legit chia tổng fraud; false decline là legit được quyết định fraud chia tổng legit; review là tập rỗng hoặc đa nhãn chia tổng số mẫu.
- Báo coverage theo lớp, false approval / false decline / review, trước và sau drift. Không gọi singleton là chắc chắn đúng; undercoverage sau drift là kết quả cần công khai, không chỉnh test để ép đạt 1−α.
- Bảo đảm theo lớp của conformal giao dịch **không** chuyển thành bảo đảm cho triage incident.

## 4. Metric và baseline

- PR-AUC, precision@k (top cảnh báo/ngày), chi phí bỏ sót vs chặn nhầm, số giao dịch vào review.
- Baseline: logistic regression; LightGBM không calibration.

## 5. Xong khi

Audit as-of và split đạt; có logistic regression; observed-label và oracle evaluation tách riêng; coverage theo lớp trước/sau drift; false approval/decline/review. *Mở rộng:* MLflow.

CLI `model score` và `model evaluate` lấy thời lượng monitoring từ `episode.duration_days` trong cấu hình simulator công khai; hai lệnh không cần `configs/episodes.yaml`.

`model evaluate` mặc định chỉ chấm episode development. Cả CLI standalone và harness đều từ chối calibration/test trước khi đọc private truth, ghi report hoặc cập nhật role ledger. Held-out model evaluation đang bị tắt cho tới khi model artifacts được content-hash bind vào frozen identity; cần follow-up trước pilot. Development vẫn được phép.

Trong harness, `model evaluate` chỉ nhận đường dẫn `--simulator-config` đã được lock/run identity bao phủ; custom path bị từ chối trước khi mở private truth.

> *Số đo bản 24/10 (đã rollback, split cũ train 7–10/calibration 11):* PR-AUC 0,92–0,93 trước drift, 0,48 sau drift; social engineering lọt 99%; tỉ lệ review không phản ứng với drift.
