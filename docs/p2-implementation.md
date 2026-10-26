# P2 — Sub-plan triển khai fraud model

> Trạng thái: **P2.1 round1-done (Vòng 1 đã bàn giao); P2.2 round1-done (Vòng 1 đã bàn giao); P2.3 round1-done (Vòng 1 đã bàn giao); P2.4 round1-done (Vòng 1 đã bàn giao); P2.5 chưa triển khai**. Hợp đồng model: [model.md](model.md); dữ liệu và snapshot nhãn: [data.md](data.md). File này sở hữu thứ tự task và nghiệm thu P2; không chép hợp đồng.
> Format task, trạng thái và luật hai vòng giống [p1-implementation.md](p1-implementation.md). Điều phối: [orchestration.md](orchestration.md).

## Thứ tự hai vòng

| Task | Vòng 1 — luồng mỏng | Vòng 2 |
|---|---|---|
| P2.1 Đọc operational data + audit split | ✅ round1-done | — |
| P2.2 Feature as-of | Tối thiểu: vài feature tần suất/số tiền theo cửa sổ | Đủ feature, gồm phía người nhận |
| P2.3 Model + calibration | ✅ round1-done — LightGBM + isotonic | Logistic regression baseline, tuning trên validation |
| P2.4 Conformal giao dịch | ✅ round1-done — Vòng 1 đã bàn giao | — |
| P2.5 Đánh giá + `model_scores` | Xuất `model_scores` cho smoke; PR-AUC, coverage theo lớp | Observed-label vs oracle, trước/sau drift, reliability diagram |

## Bố trí file dự kiến

```text
model/        __init__.py, data.py, features.py, train.py, calibrate.py, conformal.py, evaluate.py, __main__.py
tests/        test_model_split.py, test_model_features_asof.py, test_model_conformal.py, test_model_scores.py
data/<run>/model/   model artifact, model_scores, reports/  (không đưa vào git)
```

## Các task

### P2.1 — Đọc operational data và audit split (`round1-done`)
- **Mục tiêu:** chỉ đọc dữ liệu operational của P1 qua hàm as-of; dựng 3 tập theo [model.md](model.md) mục 1.
- **Phụ thuộc:** API simulator P1.1–P1.5 để dựng smoke operational và snapshot nhãn.
- **Kiểm tra:** mọi cohort có nhãn đủ tuổi ≥ H tại T0; các tập không chồng nhau; pending không thành 0; báo số lượng và số positive từng tập; thiếu support → dừng và báo, KHÔNG tự đổi split.
- **Hoàn tất:** báo cáo audit split; test split xanh. Model KHÔNG đọc file trong `private/`.
- **Trạng thái:** round1-done — Vòng 1 đã bàn giao. Smoke audit tại T0: fit `n=371`, positive `85`, negative `286`; validation `n=54`, positive `14`, negative `40`; calibration `n=152`, positive `24`, negative `128`.

### P2.2 — Feature as-of (`round1-done`, Vòng 1 đã bàn giao)
- **Mục tiêu:** feature theo cửa sổ thời gian tính bằng SQL DuckDB, đúng as-of.
- **Phụ thuộc:** P2.1.
- **Kiểm tra:** thêm giao dịch khả dụng sau t không đổi feature tại t; cửa sổ chưa đủ quan sát được đánh dấu; không feature nào đến từ truth/metadata generator.
- **Vòng 1:** `txn_count` 1h/24h/7d, tổng/trung bình amount 7d, amount chia trung bình lịch sử customer; chưa có feature phía người nhận.
- **Hoàn tất:** test as-of của feature xanh trên fixture và smoke.
- **Ma trận vòng 1:** fit/validation/calibration có số dòng khớp split; test cờ cửa sổ, nguồn dữ liệu và tính tất định xanh.
- **Trạng thái:** round1-done — Vòng 1 đã bàn giao.

### P2.3 — Model và calibration (`round1-done`, Vòng 1 đã bàn giao)
- **Mục tiêu:** LightGBM tất định trên tập fit; early stopping trên validation; isotonic trên tháng 10.
- **Phụ thuộc:** P2.2.
- **Kiểm tra:** cùng seed → cùng score; isotonic fit chỉ trên calibration; reliability diagram KHÔNG tính trên tháng 10.
- **Hoàn tất:** model artifact + metadata (`model_version`, `label_policy_version`, seed).
- **Trạng thái:** round1-done — Vòng 1 đã bàn giao.

### P2.4 — Conformal giao dịch (`round1-done`, Vòng 1 đã bàn giao)
- **Mục tiêu:** Mondrian conformal trên **score thô**, ba vùng legit / fraud / review.
- **Phụ thuộc:** P2.3.
- **Kiểm tra:** conformal đọc score thô, không đọc output isotonic; tập rỗng/đa nhãn → review; coverage theo lớp trên dữ liệu calibration-style giữ riêng ≈ 1−α trong sai số mẫu. α là cấu hình.
- **Vòng 1:** α theo lớp, quantile hữu hạn mẫu và cách ghi nhận thiếu support theo [model.md](model.md) mục 3; report kèm coverage theo lớp và các tỷ lệ hành động với mẫu số.
- **Hoàn tất:** test conformal xanh; báo coverage theo lớp.
- **Vòng 1 đã bàn giao — fixture holdout i.i.d. hiện có** (seed `20261026`; 50.000 mẫu calibration và 50.000 holdout): support calibration legit `40.031`, fraud `9.969`; support holdout legit `39.951`, fraud `10.049`. Coverage legit `0,994393` (`39.727/39.951`), fraud `0,803463` (`8.074/10.049`); false approval `0,054533` (`548/10.049 fraud`), false decline `0,000375` (`15/39.951 legit`), review `0,032720` (`1.636/50.000`). Đây là số từ fixture kiểm tra exchangeable giữ riêng, không dùng để tune α.
- **Smoke calibration in-sample** (chỉ mô tả): support legit `128`, fraud `24`; coverage legit `1,000000` (`128/128`), fraud `0,833333` (`20/24`); false approval `0,166667` (`4/24 fraud`), false decline `0` (`0/128 legit`), review `0,177632` (`27/152`). Legit `n=128 < 199` cần cho `α_legit=0,005`, report đánh dấu `insufficient_support`; áp dụng chính sách tại [model.md](model.md) mục 1 trước khi dùng kết quả. Số in-sample này không phải coverage holdout hợp lệ.
- **Trạng thái:** round1-done — Vòng 1 đã bàn giao.

### P2.5 — Đánh giá và xuất `model_scores` (`todo`)
- **Mục tiêu:** chấm điểm giai đoạn monitoring của từng episode, xuất `model_scores` cho P3.
- **Phụ thuộc:** P2.4; episode của P1.
- **Kiểm tra:** score theo đúng episode và as-of; metric đánh giá dùng truth CHỈ trong report evaluator, không ghi vào `model_scores`.
- **Hoàn tất:** `model_scores` + report; CLI `python -m model train|score|evaluate`; cập nhật Trạng thái MASTER.

## Nghiệm thu P2
- [x] Split đúng protocol, có audit support.
- [ ] Feature và score đúng as-of, không chạm truth.
- [x] Conformal trên score thô, coverage theo lớp được báo.
- [ ] `model_scores` đủ cho P3; test runtime xanh.
