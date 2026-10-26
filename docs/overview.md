# PayGuard — Tổng quan

> Fraud Monitoring + Uncertainty-Aware Investigation Agent. Project portfolio cho vị trí Data Scientist ngành thanh toán.

## Business problem

**Mục tiêu không phải tính mới, mà là giải quyết một vấn đề vận hành có thật và đo được.**

Model fraud trong production sinh nhiều alert drift, phần lớn vô hại (mùa vụ, khuyến mãi, merchant lớn mới vào), trong khi nhãn gian lận về chậm 30–90 ngày nên không kiểm chứng ngay được. Điều tra mỗi alert tốn hàng giờ của data scientist (viết SQL, chia segment, so cùng kỳ), và chỉ vài người đủ kinh nghiệm để phán đoán. Hậu quả: **alert fatigue** — đội vận hành dần phớt lờ alert và bỏ sót alert thật.

**Câu hỏi:** *Làm sao rút ngắn thời gian từ alert đến quyết định đúng, và tự đóng được các alert vô hại, mà không bỏ sót alert nguy hiểm?*

PSI/KS phát hiện thay đổi phân phối; CBPE ước lượng performance khi chưa có nhãn dưới các giả định của nó. Project giải quyết bước tiếp theo: **giả thuyết nguyên nhân, mức bằng chứng và routing phù hợp**, đo cả sự cố monitoring bỏ sót.

## Giải pháp — 3 tầng, rule trước, agent sau

- **Model** ([model.md](model.md)): fraud model có calibration + conformal → giao dịch không chắc chắn được chuyển người duyệt.
- **Monitoring** ([monitoring.md](monitoring.md)): phát hiện thay đổi và ước lượng suy giảm khi chưa có nhãn.
- **Triage** ([triage.md](triage.md)): rule xử lý sự cố đã biết; agent điều tra và đề xuất routing cho phần còn lại. Kiểm tra bằng chứng chung, nhánh đóng có gate riêng; agent chỉ được tự đóng trong runbook benign hiện có. Thiếu bằng chứng → recheck có thời hạn hoặc chuyển người.
- **Đánh giá** ([evaluation.md](evaluation.md)) trên benchmark mô phỏng có nhãn trễ ([data.md](data.md)). MVP báo kết quả thực nghiệm, không hứa bảo đảm lỗi gần 0.

## Ràng buộc

| Mục | Quyết định |
|---|---|
| LLM | Qwen 3.7 Flash qua OpenRouter (`qwen/qwen3.7-flash`); lý do: [rationale.md](rationale.md) mục 3 |
| Thế mạnh / yếu | Chưa vững ML cổ điển và data engineering → giữ đơn giản nhưng làm đúng |
| Thời gian | Nhanh nhất có thể, nhưng phải end-to-end |
| UQ của agent | Black-box trước (không phụ thuộc logprobs) |

## Kiến trúc

```
                 ┌──────────────────────────────────────────────┐
 Simulator ─────►│ DuckDB (bản nhanh)  /  Kafka→Spark (nâng cấp)│
 (giao dịch +    │ transactions, customers, merchants, labels   │
  kịch bản sự cố)└──────────┬───────────────────────────┬───────┘
     │                      │ feature (SQL window)      │
     │ truth (tách riêng)   ▼                           │
     │            ┌───────────────────┐                 │
     │            │ Fraud model       │ score + UQ      │
     │            │ LightGBM          ├────────────────►│ model_scores
     │            │ + isotonic        │                 │
     │            │ + conformal       │                 │
     │            └─────────┬─────────┘                 │
     │            ┌─────────▼──────────────────────────▼─┐
     │            │ Monitoring (job hằng ngày)           │
     │            │ PSI/KS · score/UQ · CBPE · tỉ lệ review│
     │            └─────────┬────────────────────────────┘
     │                      │ alert → incident
     │            ┌─────────▼────────────────────────────┐
     │            │ Rule triage → Investigation Agent    │
     │            │ tools: run_sql (UQ) · drift_report   │
     │            │        segment_breakdown · shap      │
     │            │ → checklist + gate → đóng/recheck/route│
     │            └─────────┬────────────────────────────┘
     │                      │ decisions
     └──────────────► Evaluator (chỉ module này đọc truth)

             FastAPI · Streamlit (dashboard + chat + incident report) · pytest + CI
```

## Rủi ro

| Rủi ro | Xử lý |
|---|---|
| Uncertainty không báo sớm với concept drift | Báo cáo trung thực theo từng kiểu drift |
| Chi phí API do sample nhiều | K = N = 3, cache đúng snapshot; adaptive N là ablation |
| Nhãn trễ/không bao giờ báo làm lệch calibration | Observed-label và oracle audit riêng; H định trước, pending không âm hóa |
| Ít episode nhưng kết luận quá mạnh | Pilot có CI và mẫu số; nhánh chứng nhận tách riêng |
| Mock data bị cho là "tự dựng cho dễ" | Tham số có nguồn; kiểm định phân phối; đánh giá thêm trên PaySim / Handbook |
| Thiếu kỹ năng DE | DuckDB; Kafka/Spark/Airflow là nâng cấp |
| License dữ liệu | Ghi nguồn; không phân phối lại IEEE-CIS; tên công ty hư cấu |

## Trình bày

- **CV:** *Xây dựng fraud monitoring với nhãn trễ và investigation agent; so với rule baseline đạt X% timely routing recall, Y% unsafe truth rate trên N sự cố test ở chi phí Z; ngưỡng đồng thuận chọn trên tập calibration.* (X, Y, Z chỉ điền từ thực nghiệm; nêu rõ benchmark mô phỏng, giới hạn quyền tự đóng và độ bao phủ đánh giá.)
- **README:** sơ đồ, GIF, bảng kết quả, `docker compose up`, Design decisions, Limitations, Related work. Dùng bản tóm tắt dễ đọc; các file docs giữ chi tiết.
- **3 câu chuyện phỏng vấn:** phát hiện suy giảm khi chưa có nhãn (và khi nào thất bại) · vì sao agent chuyển người ở ca X · nếu scale lên hàng triệu giao dịch/ngày sẽ đổi gì.
- Map vào JD: ML thực tế · đánh giá trước/sau production · theo dõi suy giảm · xác suất thống kê · Python/SQL/API · Spark/Airflow/Kafka (nâng cấp) · LLM/AI Agent.
