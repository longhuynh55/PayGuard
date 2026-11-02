# Rationale — nghiên cứu và lý do thiết kế

> Vì sao chọn các phương pháp trong docs. Lập luận và phản ví dụ chi tiết của các vòng review: archive/PLAN_REVIEW.md; ví dụ chạy được: `python3 review/verify_plan_claims.py`.

## 1. Định vị

- Agent cho **root cause analysis** đã có nhiều nhưng chủ yếu cho hạ tầng (MicroRCA-Agent, OpsAgent, ARGUS) — không phải cho suy giảm của ML model.
- Survey data science agent (arXiv 2510.04023): khâu monitoring & recovery chưa được tự động hoá tốt.
- Gần nhất về "uncertainty làm tín hiệu điều khiển agent": arXiv 2509.02401, AgentAbstain, survey Oh et al. (ACL 2026).
- Lần tìm kiếm này không thấy công trình kết hợp cả ba: *model drift + agent điều tra nguyên nhân + uncertainty quyết định tự xử lý hay chuyển người* (chưa đầy đủ — kiểm tra lại trước khi viết blog/paper).

**→ Project không cần tính mới.** Giá trị nằm ở giải quyết business problem bằng phương pháp đã được kiểm chứng và **đo trung thực** agent có ích ở đâu so với rule. Mọi thành phần UQ cite là phương pháp có sẵn.

## 2. Thành phần đã có người làm

| Phát hiện | Nguồn | Dùng trong PayGuard |
|---|---|---|
| Execution consistency là confidence score tốt cho text-to-SQL, nhưng không phải xác suất đúng | Maleki et al. 2025, arXiv 2508.14056 | Tín hiệu chính của `run_sql`; N = 3 cố định; DB phản ví dụ ([triage.md](triage.md)) |
| Một DB duy nhất có thể không phân biệt hai SQL khác nghĩa | Zhong et al., EMNLP 2020 | Chấm trên nhiều DB phản ví dụ |
| Semantic entropy / node-level uncertainty cho text-to-SQL | arXiv 2602.12015; Hasson & Guo, arXiv 2511.13984 | Related work; hướng mở rộng |
| CBPE ước lượng performance khi chưa có nhãn; cần reference có nhãn; không giải quyết concept drift nói chung | NannyML docs; arXiv 2401.08348, 2505.05295 | Baseline bắt buộc ([monitoring.md](monitoring.md)) |
| Abstention có điểm phạt cho text-to-SQL | TrustSQL, arXiv 2403.15879 | Cách chấm phạt |
| Feedback điều tra viên và nhãn trễ có cơ chế chọn mẫu khác nhau; chưa tranh chấp sau thời gian chờ là một **chính sách** gán nhãn | Dal Pozzolo et al., IEEE TNNLS 2017 | Snapshot nhãn, cohort đủ tuổi H ([data.md](data.md)) |
| Chọn tập có nhãn làm lệch phân phối cần đánh giá | Zadrozny, ICML 2004 | Lấy nguyên cohort theo tuổi; feedback sớm là ablation |
| Calibrator phải fit trên dữ liệu tách khỏi fit classifier | scikit-learn docs | Split 3 tập; conformal trên score thô ([model.md](model.md)) |
| Split conformal kiểm soát coverage tổng thể, không tự cho bảo đảm trong nhóm tự đóng | Angelopoulos & Bates 2021 | Đo rủi ro trong nhóm tự đóng riêng ([evaluation.md](evaluation.md)) |
| KnowNo có bảo đảm dưới phân phối tình huống và cơ chế trợ giúp người được giả định; hỏi người khi tập không phải singleton | Ren et al., CoRL 2023 | Nhánh mở rộng của agent |
| Selective risk định nghĩa trên phần được chọn, đánh đổi với coverage | Geifman & El-Yaniv, NeurIPS 2017 | Báo dangerous closure rate và unsafe fraction riêng |
| Chọn policy rồi tính CI trên cùng tập cần kiểm soát nhiều giả thuyết | Learn then Test, arXiv 2110.01052 | Tune trên calibration, đánh giá trên test giữ riêng |
| p-value không cho xác suất giả thuyết đúng | ASA statement | Mẫu nhỏ/p-value lớn không đủ để tự đóng |
| Point-in-time retrieval | Feast docs | Hợp đồng as-of ([data.md](data.md)) |
| Exact binomial cho 0 lỗi | NIST | Nhánh chứng nhận 299 episode |
| Simulator giao dịch thẻ có kịch bản fraud theo thời gian | Le Borgne et al., *Fraud Detection Handbook* | Nền cho simulator, tự viết lại |

Các tham số H = 90, K = N = 3, chia 12/24/24, D = 7 và ngưỡng 1% là **lựa chọn thiết kế benchmark**, không phải chuẩn nghiệp vụ suy ra từ các nguồn trên.

## 3. LLM: Qwen 3.7 Flash qua OpenRouter

- **Chốt 26/10/2026 (người dùng):** `qwen/qwen3.7-flash` trên OpenRouter, thay DeepSeek V4 Flash (chọn 24/10). Người dùng yêu cầu "DeepSeek V5 Flash" nhưng OpenRouter không có model này (bản Flash mới nhất là `deepseek/deepseek-v4.1-flash`).
- Theo API `openrouter.ai/api/v1/models` ngày 26/10/2026: canonical slug `qwen/qwen3.7-flash-20260727`; giá 0,03 USD/1M token input, 0,13 USD/1M output; nhà cung cấp duy nhất Alibaba; hỗ trợ `tools`, `tool_choice`, `response_format`, `seed`, `logprobs`. **Không** có `structured_outputs` (JSON schema chặt) → code phải tự kiểm tra JSON, sai format → `human_review` ([triage.md](triage.md)).
- `logprobs` có trên endpoint nên baseline confidence theo logprob ([triage.md](triage.md) mục 5) dùng được; plan vẫn không phụ thuộc nó.
- Cache key và `policy_version` ghi canonical slug, không chỉ ID ngắn, vì ID ngắn có thể trỏ sang snapshot mới. Đổi snapshot = policy version mới.
- **Thử API 26/10 (1 câu SQL đếm giao dịch theo ngày giờ VN):** mặc định model suy luận ẩn và dùng hết `max_tokens` = 300 vào reasoning, `content` rỗng; `reasoning.effort = low` vẫn tiêu hết 1500 token mà không trả lời. `reasoning.enabled = false` trả JSON đúng (`DATE(created_at, '+7 hours')`), 56 token vào / 34 token ra, khoảng 6e-6 USD. → Cấu hình reasoning phải khai báo tường minh trong sampling config (thuộc cache key/`policy_version`); `content` rỗng hoặc hết token → lỗi định dạng → `human_review`.
- Chất lượng SQL tiếng Việt trên schema PayGuard chưa đo; đo ở P4.6 ([p4-implementation.md](p4-implementation.md)) trước khi khóa policy.

## 4. Điểm dừng research

Research cho MVP dừng ở đây. Chỉ mở thêm khi label audit không hỗ trợ protocol, confidence không phân tách đúng/sai, hoặc cần chứng minh loại rủi ro khác. Các ví dụ trong `review/` xác nhận phép tính và tính nhất quán của protocol, không thay cho việc chạy PayGuard.

## 5. Tài liệu tham khảo

★ = nên đọc trước.

**UQ cho LLM / agent**
- ★ Kirchhof, Kasneci, Kasneci — *Position: UQ Needs Reassessment for LLM Agents*, ICML 2025 — https://arxiv.org/abs/2505.22655
- Oh et al. — *Uncertainty Quantification in LLM Agents*, ACL 2026 — https://preview.aclanthology.org/ingest-acl/2026.acl-long.738/
- Liu et al. — *UQ and Confidence Calibration in LLMs: A Survey*, KDD 2025 — https://arxiv.org/abs/2503.15850
- ★ Ren et al. — *Robots That Ask For Help (KnowNo)*, CoRL 2023 — https://proceedings.mlr.press/v229/ren23a.html
- *Towards Agents That Know When They Don't Know*, NeurIPS 2025 workshop — https://arxiv.org/abs/2509.02401
- *AgentAbstain* — https://www.alphaxiv.org/abs/2607.10059
- Angelopoulos & Bates — *A Gentle Introduction to Conformal Prediction* — https://arxiv.org/abs/2107.07511
- Geifman & El-Yaniv — *Selective Classification for Deep Neural Networks* — https://arxiv.org/abs/1705.08500
- Angelopoulos et al. — *Learn then Test* — https://arxiv.org/abs/2110.01052
- Zadrozny — *Learning and Evaluating Classifiers under Sample Selection Bias* — https://research.ibm.com/publications/learning-and-evaluating-classifiers-under-sample-selection-bias

**Text-to-SQL có uncertainty**
- ★ Maleki et al. — *Confidence Estimation for Text-to-SQL in LLMs* — https://arxiv.org/abs/2508.14056
- TrustSQL — https://arxiv.org/abs/2403.15879
- Hasson & Guo — node-level uncertainty — https://arxiv.org/abs/2511.13984
- Semantic-entropy cho text-to-SQL — https://arxiv.org/abs/2602.12015
- Zhong et al. — test-suite evaluation, EMNLP 2020 — https://aclanthology.org/2020.emnlp-main.29/

**Monitoring khi chưa có nhãn**
- ★ NannyML — CBPE — https://nannyml.readthedocs.io/en/main/how_it_works/performance_estimation.html
- *Estimating Model Performance Under Covariate Shift Without Labels* — https://arxiv.org/abs/2401.08348
- Rabanser et al. — *Failing Loudly*, NeurIPS 2019 — https://arxiv.org/abs/1810.11953

**Fraud detection**
- ★ Dal Pozzolo et al. — *Credit Card Fraud Detection: A Realistic Modeling and a Novel Learning Strategy*, IEEE TNNLS — https://boracchi.faculty.polimi.it/docs/2017_FraudsTNNLS.pdf
- ★ Le Borgne et al. — *Fraud Detection Handbook* — https://github.com/Fraud-Detection-Handbook/fraud-detection-handbook

**Agent điều tra sự cố / data science agent**
- *LLM-Based Data Science Agents: A Survey* — https://arxiv.org/abs/2510.04023
- ARGUS — MCP-grounded RCA cho Kubernetes — https://arxiv.org/abs/2608.23084
- Yao et al. — *ReAct* — https://arxiv.org/abs/2210.03629

**Khác**
- ASA statement on p-values — https://www.amstat.org/asa/files/pdfs/p-valuestatement.pdf
- Feast — point-in-time joins — https://docs.feast.dev/getting-started/concepts/point-in-time-joins
- NIST — exact binomial — https://itl.nist.gov/div898/software/dataplot/refman2/auxillar/exacbino.htm
