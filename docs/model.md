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
- Target chính là nhãn theo policy; pending không dùng làm 0. *Mở rộng:* oracle-label ablation; khi có `disputed`, loại khỏi supervised fit và báo mức độ loại mẫu.
- Calibration xác suất: isotonic (sigmoid nếu ít support) trên tháng 10, giữ nguyên tỉ lệ lớp của cohort. Reliability diagram trên observed-label và oracle truth ghi riêng.

## 3. Conformal giao dịch

- Ba vùng: singleton legit / singleton fraud / review; tập rỗng và đa nhãn đều review.
- Mondrian alpha theo lớp là cấu hình thực nghiệm. Bản 24/10 dùng α bất đối xứng (α_legit 0,005, α_fraud 0,20) vì α bằng nhau chặn ~10% giao dịch hợp lệ và α_fraud 0,10 đẩy ~9% vào review — cần đo lại trên split mới.
- Báo coverage theo lớp, false approval / false decline / review, trước và sau drift. Không gọi singleton là chắc chắn đúng; undercoverage sau drift là kết quả cần công khai, không chỉnh test để ép đạt 1−α.
- Bảo đảm theo lớp của conformal giao dịch **không** chuyển thành bảo đảm cho triage incident.

## 4. Metric và baseline

- PR-AUC, precision@k (top cảnh báo/ngày), chi phí bỏ sót vs chặn nhầm, số giao dịch vào review.
- Baseline: logistic regression; LightGBM không calibration.

## 5. Xong khi

Audit as-of và split đạt; có logistic regression; observed-label và oracle evaluation tách riêng; coverage theo lớp trước/sau drift; false approval/decline/review. *Mở rộng:* MLflow.

> *Số đo bản 24/10 (đã rollback, split cũ train 7–10/calibration 11):* PR-AUC 0,92–0,93 trước drift, 0,48 sau drift; social engineering lọt 99%; tỉ lệ review không phản ứng với drift.
