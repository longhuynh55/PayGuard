# PayGuard — Fraud Monitoring + Uncertainty-Aware Investigation Agent

> **Lưu trữ (25/10/2026):** nội dung đã chuyển sang `docs/` theo module; file này không còn được cập nhật. Liên kết bên trong trỏ tới vị trí cũ.

> Kế hoạch project portfolio cho vị trí Data Scientist (ngành thanh toán).
> Phiên bản 2 — 25/10/2026. Protocol hiện hành: [MASTER_PLAN.md](MASTER_PLAN.md) mục 4 và [RESEARCH_DECISIONS.md](RESEARCH_DECISIONS.md). Code P1/P2 bản 24/10 đã rollback; viết lại theo protocol mới.

---

## 0. Business problem

**Mục tiêu của project không phải tính mới, mà là giải quyết một vấn đề vận hành có thật và đo được.**

> Model fraud trong production sinh ra nhiều alert drift, phần lớn là vô hại (mùa vụ, khuyến mãi, merchant lớn mới vào), trong khi nhãn gian lận về chậm 30–90 ngày nên không kiểm chứng ngay được. Điều tra mỗi alert tốn hàng giờ của data scientist (viết SQL, chia theo segment, so cùng kỳ), và chỉ vài người đủ kinh nghiệm để phán đoán. Hậu quả: **alert fatigue** — đội vận hành dần phớt lờ alert và bỏ sót alert thật.

**Câu hỏi project trả lời:** *Làm sao rút ngắn thời gian từ alert đến quyết định đúng, và tự đóng được các alert vô hại, mà không bỏ sót alert nguy hiểm?*

PSI/KS phát hiện thay đổi phân phối; CBPE ước lượng performance khi analysis chưa có nhãn dưới các giả định của phương pháp. Project giải quyết bước tiếp theo: **giả thuyết nguyên nhân, mức bằng chứng và routing phù hợp**; đo cả sự cố monitoring bỏ sót.

### Giải pháp — 3 tầng, rule trước, agent sau
- **Tầng model:** fraud model có calibration + conformal → giao dịch không chắc chắn được chuyển người duyệt.
- **Tầng monitoring:** phát hiện thay đổi và ước lượng suy giảm khi chưa có nhãn (PSI/KS, CBPE).
- **Tầng triage (hybrid):**
  1. *Rule-based triage* xử lý các kiểu sự cố đã biết (lịch mùa vụ, đổi thang đo, segment đóng góp chính) — rẻ, nhanh, dễ kiểm soát.
  2. *Investigation agent* nhận các alert rule không xử lý được hoặc không chắc → tự điều tra bằng truy vấn dữ liệu, kết luận kèm độ tin cậy.
  3. *Uncertainty quyết định:* rule và agent cùng qua gate bằng chứng + score; ngưỡng chọn trên calibration, đánh giá rủi ro trên test giữ riêng. Thiếu bằng chứng → recheck có thời hạn hoặc chuyển người. MVP báo cáo thực nghiệm, chưa hứa bảo đảm lỗi gần 0.

**Một câu cho CV:** *Hệ thống fraud monitoring triage alert drift bằng rule và investigation agent; đánh giá tự đóng/chuyển người theo risk–coverage và chi phí trên benchmark mô phỏng có nhãn trễ.*

Map vào JD: ML/DL thực tế ✔ · đánh giá trước/sau production ✔ · theo dõi & phát hiện suy giảm ✔ · xác suất thống kê ✔ · Python/SQL/API ✔ · Spark/Airflow/Kafka (bản nâng cấp) ✔ · LLM/AI Agent ✔.

---

## 1. Ràng buộc đã chốt

| Mục | Quyết định |
|---|---|
| LLM | DeepSeek V4 Flash (API) |
| Thế mạnh / yếu | Chưa vững ML cổ điển và data engineering → giữ đơn giản nhưng làm đúng |
| Thời gian | Nhanh nhất có thể, nhưng phải end-to-end |
| Phương pháp UQ của agent | **Black-box trước** (không phụ thuộc logprobs) |

---

## 2. Kết quả research — điều gì đã được kiểm chứng, điều gì thay đổi plan

### 2.1 Những thứ ĐÃ CÓ người làm → dùng làm thành phần / baseline, không nhận là "mới"

| Phát hiện | Nguồn | Ảnh hưởng tới plan |
|---|---|---|
| **Execution consistency** là confidence score có kết quả thực nghiệm tốt trong nghiên cứu text-to-SQL, nhưng không phải xác suất đúng sẵn có | Maleki et al. 2025, arXiv 2508.14056 | Dùng làm tín hiệu chính; N=3 cố định trước, adaptive N là ablation; kiểm tra DB phản ví dụ |
| Semantic entropy cho text-to-SQL; uncertainty ở mức node AST | arXiv 2602.12015 (2026); Hasson & Guo, arXiv 2511.13984 | Đưa vào phần related work; node-level là hướng mở rộng |
| **Ước lượng performance khi chưa có nhãn** bằng calibrated confidence (CBPE) đã có trong thư viện NannyML; điểm yếu đã biết: calibration hỏng dưới một số kiểu shift | NannyML docs; arXiv 2401.08348, 2505.05295 | **CBPE là baseline bắt buộc** cho phần "phát hiện suy giảm trước khi có nhãn". Không nhận ý tưởng này là mới |
| Abstention có tính điểm phạt cho text-to-SQL | TrustSQL, arXiv 2403.15879 (CC BY-SA 4.0) | Dùng cách chấm điểm phạt cho agent |
| Conformal để LLM planner biết khi nào hỏi người, với giả định phân phối và trợ giúp người trong bài | KnowNo, Ren et al. CoRL 2023 | Nhánh bổ sung của agent: tập không phải singleton → hỏi người; không đồng nhất coverage với rủi ro trong nhóm tự đóng |
| Fraud thực tế: concept drift + mất cân bằng + **nhãn về chậm**; nên tách feedback của điều tra viên và nhãn về chậm | Dal Pozzolo et al., IEEE TNNLS 2017 | Thiết kế bảng nhãn có `reported_at` và `source`; đánh giá theo đúng độ trễ nhãn |
| Có sẵn **simulator giao dịch thẻ** sinh dữ liệu với kịch bản fraud theo thời gian | Le Borgne et al., *Fraud Detection Handbook* (ULB + Worldline) | **Dùng làm nền cho mock data** rồi tùy biến bối cảnh VN (license repo không rõ → tự viết lại theo ý tưởng, ghi nguồn) |

### 2.2 Khoảng trống (gap) — phần đóng góp riêng của project

- Agent cho **root cause analysis** đã có nhiều, nhưng chủ yếu cho **hạ tầng** (microservices, Kubernetes): MicroRCA-Agent, OpsAgent, ARGUS (arXiv 2608.23084)… — không phải cho **suy giảm của ML model**.
- Survey về data science agent (arXiv 2510.04023) nhận định khâu **monitoring & recovery** của agent còn chưa được tự động hoá tốt, drift detection vẫn mang tính phản ứng.
- Các công trình "uncertainty làm tín hiệu điều khiển agent" gần nhất: arXiv 2509.02401 (agent multi-table biết abstain, NeurIPS 2025 workshop), AgentAbstain (alphaxiv 2607.10059), survey Oh et al. *UQ in LLM Agents* (ACL 2026).
- Lần tìm kiếm này **không thấy** công trình nào kết hợp cả ba: *ML model drift + agent điều tra nguyên nhân + uncertainty quyết định tự xử lý hay chuyển người.* (Tìm kiếm chưa đầy đủ — cần kiểm tra lại trước khi viết blog/paper.)

**→ Định vị:** project không cần tính mới. Giá trị nằm ở **giải quyết business problem ở mục 0** bằng các phương pháp đã được kiểm chứng, và **đo trung thực** agent có ích ở đâu so với rule-based triage. Các thành phần UQ đều cite là phương pháp có sẵn.

### 2.3 Về DeepSeek V4 Flash

- Theo IT之家, API chính thức V4-Flash mở public beta 31/07/2026.
- Một số nhà cung cấp trung gian (Neura, OrcaRouter) liệt kê hỗ trợ `logprobs` / `top_logprobs`, **chưa xác nhận trên tài liệu chính thức của DeepSeek** → kiểm tra trong tuần 1. Plan không phụ thuộc vào logprobs.

### 2.4 Research có mục tiêu ngày 25/10 — quyết định triển khai

- Phân biệt nhãn quan sát theo policy với fraud truth; cohort đủ tuổi H=90 ngày cho supervised fit/calibration; pending không âm hóa. Feedback sớm là ablation riêng.
- Conformal giao dịch vẫn ở M2; agent MVP dùng selective abstention theo score thực nghiệm và gate chung với rule. KnowNo/conformal agent là nhánh bổ sung khi có đủ calibration đúng phân phối routing.
- Chọn ngưỡng trên calibration, báo risk–coverage và lỗi tự đóng trên test. 60 episode là pilot; không suy bảo đảm rủi ro 1% từ vài chục ca.
- Nguồn gốc, schema, split, score, metric và giả định thống kê: [RESEARCH_DECISIONS.md](RESEARCH_DECISIONS.md). Điểm dừng research là protocol đủ để lập trình; bước tiếp theo là audit code và chạy luồng nhỏ.

---

## 3. Kiến trúc

```
                 ┌──────────────────────────────────────────────┐
 Simulator ─────►│ DuckDB (bản nhanh)  /  Kafka→Spark (nâng cấp)│
 (giao dịch +    │ transactions, customers, merchants, labels   │
  kịch bản sự cố)└──────────┬───────────────────────────┬───────┘
                            │ feature (SQL window)      │
                  ┌─────────▼─────────┐                 │
                  │ Fraud model       │ score + UQ      │
                  │ LightGBM          ├────────────────►│ model_scores
                  │ + calibration     │                 │
                  │ + conformal       │                 │
                  └─────────┬─────────┘                 │
                            │                           │
                  ┌─────────▼──────────────────────────▼─┐
                  │ Monitoring (job hằng ngày)           │
                  │ PSI/KS feature · phân phối score/UQ  │
                  │ CBPE (baseline) · tỉ lệ cảnh báo     │
                  └─────────┬────────────────────────────┘
                            │ alert
                  ┌─────────▼────────────────────────────┐
                  │ Rule triage → Investigation Agent    │
                  │ tools: run_sql (UQ) · drift_report   │
                  │        segment_breakdown · shap      │
                  │ → giả thuyết + confidence            │
                  │ → gate chung → đóng / recheck / route │
                  └─────────┬────────────────────────────┘
                            │
             FastAPI  ◄─────┴────►  Streamlit UI (dashboard + chat + incident report)
             MLflow (mở rộng)   pytest + CI
```

---

## 4. Các module chi tiết

### M1 — Dữ liệu mock (tuần 1)
- **Nền:** ý tưởng simulator của *Fraud Detection Handbook* (profile khách hàng & terminal → sinh giao dịch theo thời gian → chèn kịch bản fraud).
- **Tùy biến bối cảnh VN:** kênh QR/ví/thẻ, MCC theo ISO 18245, ngân hàng phát hành, tỉnh/thành (HCM/HN chiếm phần lớn), giờ cao điểm, ngày lương, Tết, 11/11.
- **Schema (MVP):** `customers`, `merchants`, `transactions` (các mốc nghiệp vụ + `available_at`), label events (`label_status`, `reported_at`, `available_at`, `source`), `business_calendar` (có `expected_effect`, `scope`). Mỗi giao dịch tối đa một kết luận; snapshot có confirmed_fraud / confirmed_legit / matured_negative / pending.
  - *Mở rộng:* `disputed`, sửa nhãn append-only qua `supersedes_event_id` (cột để sẵn, NULL trong MVP).
- **As-of và truth:** feature và SQL chỉ đọc thông tin đã khả dụng tại thời điểm chạy, đúng episode; truth/injector chỉ evaluator truy cập. Cash-out sau giao dịch chỉ dùng khi đã quan sát đủ cửa sổ, hoặc đánh dấu censored.
- **Label tail:** sinh sự kiện nhãn tới ít nhất H ngày sau giao dịch cuối; không cho feature đọc giao dịch tương lai. H=90 là cấu hình MVP (*mở rộng:* H=30/60, H theo kênh); ghi rõ fraud không bao giờ báo và báo sau H ở oracle audit.
- **Config YAML** cho mọi tham số, mỗi tham số ghi nguồn (PaySim, Handbook, báo cáo thị trường).
- **Kiểm định:** so phân phối với dữ liệu tham chiếu (KS, biểu đồ chồng) → đưa vào README.

### M2 — Fraud model có uncertainty (tuần 2)
- Chia theo thời gian và tuổi cohort >=H tại T0: fit 1/7–21/9/2025; validation 22–30/9; calibration 10/2025 (isotonic + ngưỡng conformal trên score thô + reference CBPE); monitoring từ T0=1/2/2026. Audit support trước khi áp dụng, mở rộng lịch sử nếu thiếu. Lý do gộp: MASTER 4-D.
- LightGBM + xử lý mất cân bằng; feature: tần suất theo cửa sổ thời gian, số tiền so với thói quen, rủi ro terminal/merchant.
- Calibration xác suất (isotonic, sigmoid làm lựa chọn nếu ít support) trên tháng 10, giữ nguyên tỉ lệ lớp của cohort; conformal tính trên score thô nên không phụ thuộc calibrator. Reliability diagram vẽ trên dữ liệu sau T0 (không phải tháng 10), observed-label và oracle truth ghi riêng.
- **Conformal giao dịch:** singleton legit / singleton fraud / review; tập rỗng và đa nhãn đều review. Mondrian alpha theo lớp là cấu hình thực nghiệm; báo coverage theo lớp, false approval/decline và review trước/sau drift. Không gọi singleton là chắc chắn đúng; undercoverage sau drift là kết quả cần công khai.
- Mục tiêu chính là nhãn theo policy; pending không dùng làm 0. *Mở rộng:* oracle-label ablation (train/calibration bằng truth); khi có `disputed`, loại khỏi supervised fit và báo mức độ loại mẫu.
- Metric: PR-AUC, precision@k (top cảnh báo/ngày), **chi phí kinh doanh** (bỏ sót vs chặn nhầm), số case chuyển người.

### M3 — Monitoring & phát hiện suy giảm (tuần 3)
- Hằng ngày: PSI/KS trên feature, phân phối score và uncertainty, tỉ lệ cảnh báo, **CBPE** (NannyML) ước lượng performance khi chưa có nhãn.
- Reference PSI/KS có thể chưa nhãn (11/2025–1/2026); fit CBPE dùng cohort tháng 10 có nhãn theo policy và predictor đã cố định. CBPE không làm thay đổi calibrator của model/conformal.
- Khi nhãn về (trễ): tính performance thật → so với ước lượng.
- **Thí nghiệm chính:** với từng kiểu drift (covariate shift / concept drift / kiểu fraud mới / lỗi pipeline / mùa vụ), tín hiệu nào báo sớm và báo đúng? Chấp nhận kết quả âm tính (ví dụ concept drift mà model vẫn tự tin).
- Ngưỡng monitoring/matcher chỉ tune trên development; báo mọi sự cố bỏ sót trong end-to-end recall. CBPE không báo động chưa đủ để kết luận benign.

### M4a — Rule-based triage (tuần 4, ~2 ngày)
- Lịch mùa vụ (Tết, 11/11, ngày lương) → nhãn "có thể do mùa vụ" khi drift nằm trong khung thời gian đó và pattern giống cùng kỳ.
- Phát hiện đổi thang đo / giá trị bất thường đột ngột (ví dụ trung vị nhân ~100) → "nghi lỗi pipeline".
- Tự động chia theo segment (merchant, kênh, tỉnh) → segment đóng góp chính vào drift.
- **MVP:** 4 runbook viết thành checklist code — dữ liệu bắt buộc, cửa sổ phải hoàn tất, điều kiện bắt buộc chuyển người (RESEARCH 3.2a).
- Rule chỉ được viết cho **nhóm kịch bản "đã biết"** (xem 5.1). Alert không khớp rule nào, khớp nhiều rule mâu thuẫn, hoặc khớp runbook đang tắt quyền đóng → chuyển cho agent.
- Rule tạo kết luận ứng viên; mọi đề xuất đóng qua gate như agent. Lịch mùa vụ/p-value/cỡ mẫu không đủ tự đóng. Phải kiểm tra seasonal+fraud và mẫu nhỏ trước khi đánh giá pilot.

### M4b — Investigation Agent (tuần 4–5)
- Vòng lặp kiểu ReAct, kích hoạt bởi alert rule không xử lý được, hoặc câu hỏi của người vận hành (tiếng Việt).
- Kết quả của rule được đưa vào làm ngữ cảnh ban đầu cho agent.
- **Tools:**
  - `run_sql(question)` — N=3 SQL cố định, chạy và gom cụm theo kết quả; consistency là score. Adaptive N là ablation sau, chạy lại calibration/test theo policy mới.
  - `drift_report(date_range)`, `segment_breakdown(metric, dims)`, `explain_txn(txn_id)` (SHAP).
- **Đầu ra:** cause_hypotheses theo MASTER 4-C + evidence_status (sufficient/insufficient/conflicting) + action (close/recheck/data_ticket/fraud_ops/human_review). Mẫu nhỏ không là lớp truth benign; nhiều giả thuyết và nhiều nguyên nhân thật được diễn đạt riêng.
- **UQ MVP:** K=3 lượt; close_score=min(tỉ lệ kết luận benign, min consistency của các SQL liên quan). Lượt lỗi không hỗ trợ benign, SQL lỗi score 0; không nhân score để coi là xác suất toàn chuỗi. Gate loại đề xuất thiếu/mâu thuẫn bằng chứng, snapshot sai và rủi ro nghiêm trọng chưa giải quyết.
- **Chọn policy (RESEARCH 3.2b):** bước 1 chọn on/off quyền tự đóng cho từng runbook; bước 2 chọn tau_agent ∈ {2/3,1} trên phần ca thực sự tới agent: tối đa benign đóng đúng với zero unsafe closure quan sát, hòa lấy tau lớn hơn; không có tau hữu ích thì agent_close_disabled (agent vẫn routing). Một runbook lỗi không tắt agent. Đây là tuning thực nghiệm, test giữ riêng đánh giá rủi ro.
- **Bằng chứng:** agent chỉ đề xuất nguyên nhân và runbook; `evidence_status` do checklist chạy trên snapshot tính. Lưu kết quả checklist + tool trace kèm kết luận agent.
- **Recheck:** tối đa hai lần cách nhau một ngày rồi chuyển người; lưu workload và unresolved. Singleton nguy hiểm vẫn routing. *Mở rộng:* nhánh conformal agent/KnowNo: tập rỗng/đa nhãn chuyển người, không dùng coverage như chứng nhận lỗi tự đóng.
- **Thống kê:** bootstrap theo episode khi incident phụ thuộc; exact binomial chỉ khi đơn vị thử và giả định phù hợp. Mẫu số 0 là NA. ECE nhiều bin không đáng tin với pilot rất nhỏ; ưu tiên risk–coverage và bảng số đếm.
- **Cache:** gồm prompt/model/sampling config/schema/episode/as-of/snapshot; replay không tính như mẫu mới. Agent không được truy cập truth qua tool SQL/file.

### M5 — Đóng gói (tuần 6)
- FastAPI (score, monitoring, agent), Streamlit (dashboard drift, danh sách sự cố, báo cáo của agent, chat).
- pytest, Docker Compose, README + video 3 phút. *Mở rộng:* MLflow.

### Nâng cấp nếu còn thời gian
Kafka stream thật → Spark Structured Streaming feature → Airflow DAG (retrain, monitor, recalibrate) · ensemble cho epistemic uncertainty · Next.js UI · user study nhỏ.

---

## 5. Thiết kế đánh giá

> Taxonomy, ma trận chi phí, cách sinh episode và định nghĩa alert đã chốt ở [MASTER_PLAN.md](MASTER_PLAN.md) mục 4 (A–D) — nơi đó là bản chính khi có khác biệt với mục này.

### 5.1 Benchmark kịch bản sự cố (đóng góp chính)
Mỗi kịch bản được chèn vào dữ liệu mock tại một thời điểm, có **ground truth nguyên nhân**:

| Kịch bản | Nguyên nhân thật | Hành vi mong đợi |
|---|---|---|
| Giao dịch tăng mạnh dịp Tết | `seasonal` | Benign ứng viên; chỉ tự đóng qua gate |
| Card testing tăng đột biến (kiểu đã có trong train) | `fraud_surge` | Báo fraud ops |
| `amount` nhân 100 từ một ngày | Lỗi pipeline (đổi đơn vị) | Báo đội data |
| Merchant mới, ít giao dịch, bằng chứng chưa đủ | Truth độc lập với cỡ mẫu | Recheck/chuyển người; có fraud thật thì vẫn chấm theo truth fraud |
| Fraud mới + Tết cùng lúc | Nhiều nguyên nhân | Chuyển người |
| Kẻ gian bắt chước hành vi bình thường | Concept drift "im lặng" | Kiểm tra agent có phát hiện không (ca khó) |

- **Quy mô pilot:** 60 episode, 20 clean và 40 injected; 4 episode hai nguyên nhân. 12 development / 24 calibration / 24 test ở cấp episode; clean chia 4/8/8. Nếu seed đã được xem để thiết kế, sinh seed mới cho held-out.
- **Known/unknown:** lưu rule_covered và mechanism_seen_in_calibration riêng; seed mới cùng template đo giá trị ngoài rule, không chứng minh gặp cơ chế hoàn toàn mới. Stress test cơ chế giữ riêng là thí nghiệm bổ sung.
- Development cho rule/prompt/monitoring/matcher/score và ngưỡng runbook; calibration chỉ chọn quyền đóng từng runbook rồi tau agent; test policy đã khóa. Incident con + chấm theo `decision_at` theo RESEARCH 3.3a. Truth có entity/segment/feature/hiệu ứng cửa sổ và required_actions; matcher giữ ambiguous/unmatched, không tự gán benign.
- **Business metric (chính):**
  - Tỉ lệ alert được tự xử lý đúng (giảm tải cho người vận hành).
  - Dangerous closure rate (đóng sai / incident cần can thiệp tới triage) và unsafe fraction among closures (đóng sai / tổng tự đóng); nêu cả số đếm, nguồn rule/agent và CI phù hợp. Pilot chỉ báo kết quả quan sát.
  - Detection và timely-routing recall trên **mọi sự cố cần can thiệp**, kể cả monitoring bỏ sót; đúng hạn = action ∈ `accepted_actions` trước `deadline_at` (RESEARCH 3.3), recheck không tính; required_actions cho nhiều nguyên nhân không ép một nhãn nghiêm trọng nhất cho mọi alert.
  - Chi phí và thời gian cho mỗi lần điều tra.
- **Kỹ thuật metric:** accuracy nguyên nhân (tách theo nhóm đã biết / chưa biết) · ECE/AUROC của confidence · risk–coverage · thời gian phát hiện (ngày) · tổng điểm theo ma trận chi phí (mục 9).
- **Kết luận chấp nhận được cả hai chiều:** nếu agent không hơn rule ở nhóm "chưa biết", báo cáo trung thực khi nào rule là đủ.
- *Mở rộng:* nhánh chứng nhận dùng loss nhị phân theo episode nguy hiểm độc lập, policy cố định: zero lỗi cần 299 episode để cận trên một phía 95% <=1%. Không thay mẫu số bằng số alert, không tune/đọc test nhiều lần tới khi đạt CI; bảo đảm giới hạn trong phân phối benchmark.

### 5.2 Bộ câu hỏi SQL (cho tool `run_sql` và phần chat)
- 100–150 câu, format JSON: `question`, `category` (clear / ambiguous / unanswerable / statistical_trap), `interpretations` (danh sách SQL đúng), `expected_behavior` (answer / clarify / abstain).
- Tỉ lệ chốt: rõ ràng 40% · mơ hồ 30% · không trả lời được 15% · bẫy thống kê 15%.
- Xây: template có tham số → DeepSeek paraphrase kiểu người Việt hỏi → **tự review**.
- Chấm kết quả trên nhiều DB phản ví dụ (sai mẫu số/join, duplicate, NULL, thời điểm biên, pending labels); quy định rõ absolute/relative tolerance theo metric. Chấm clarify/abstain riêng; SQL đồng kết quả trên một DB chưa chứng minh đúng nghĩa.
- Tuỳ chọn: **user simulator** (LLM biết ý định ẩn) để chấm luồng hỏi lại.

### 5.3 Baseline phải so sánh
| Phần | Baseline |
|---|---|
| Fraud model | Logistic regression; LightGBM không calibration |
| Phát hiện suy giảm | Chỉ theo dõi nhãn (trễ) · PSI/KS thuần · **CBPE (NannyML)** |
| Confidence SQL | Verbalized confidence · logprob (nếu API có) · self-consistency theo chuỗi SQL |
| Triage | **Rule + human (baseline chính)** · cùng rule + agent không UQ · luôn chuyển người · cùng rule + agent có UQ; cùng monitoring, dữ liệu as-of và báo chi phí sampling. Agent không UQ = cùng kết luận đa số + bằng chứng của 3 trace, bỏ điều kiện `close_score >= tau`; thêm agent một lượt chạy thật để đo giá trị vận hành (RESEARCH 3.5) |

---

## 6. Lộ trình hiện hành

Theo [MASTER_PLAN.md](MASTER_PLAN.md) mục 3. Bước đầu là viết lại P1 (simulator theo schema as-of; injector ghi luôn `accepted_actions`, `intervention_start_at`, `deadline_at`) rồi P2 theo split 3 tập. Sau đó làm luồng nhỏ snapshot → score → monitor → rule → gate → report, rồi thêm agent một bước. Research cho MVP dừng ở protocol hiện hành; không chặn triển khai bằng một vòng literature review rộng hơn.

---

## 7. Rủi ro & cách xử lý

| Rủi ro | Xử lý |
|---|---|
| Uncertainty không báo sớm với concept drift | Báo cáo trung thực theo từng kiểu drift; đó là kết quả có giá trị |
| Chi phí API do sample nhiều | K=N=3 và cache đúng snapshot; adaptive N/model rẻ là ablation, thay đổi policy phải đánh giá lại |
| Nhãn trễ/không bao giờ báo làm lệch calibration | Observed-label và oracle audit riêng; H định trước, pending không âm hóa |
| Ít episode nhưng kết luận quá mạnh | Pilot có CI và mẫu số; nhánh chứng nhận tách riêng, xử lý phụ thuộc theo episode |
| Mock data bị cho là "tự dựng cho dễ" | Tham số có nguồn; kiểm định phân phối; thêm đánh giá trên dataset công khai (PaySim / Handbook) |
| Thiếu kỹ năng DE | Bản nhanh dùng DuckDB; Kafka/Spark/Airflow là nâng cấp |
| License dữ liệu | Ghi nguồn; không phân phối lại IEEE-CIS; dùng tên công ty hư cấu, không dùng thương hiệu thật |

---

## 8. Cách trình bày

- **CV:** *Xây dựng fraud monitoring với nhãn trễ và investigation agent; đạt X% benign automation, Y% dangerous closure rate trên N incident test, so với rule baseline ở chi phí Z.* (Chỉ điền từ kết quả thực nghiệm; nêu benchmark mô phỏng, không suy ra tiết kiệm lao động thực tế.)
- **README:** sơ đồ, GIF, bảng kết quả, `docker compose up`, *Design decisions*, *Limitations*, *Related work*.
- **3 câu chuyện phỏng vấn:** phát hiện suy giảm khi chưa có nhãn (và khi nào thất bại) · vì sao agent chuyển người ở ca X · nếu scale lên hàng triệu giao dịch/ngày sẽ thay đổi gì.

---

## 9. Các quyết định hiện hành (25/10/2026)

1. **Tỉ lệ câu hỏi SQL** — đề xuất 40/30/15/15 (rõ ràng / mơ hồ / không trả lời được / bẫy thống kê).
2. **User simulator** — đề xuất để nâng cấp sau; bản đầu chỉ chấm agent có nhận ra câu hỏi mơ hồ không.
3. **Utility và metric** — theo MASTER 4-C; điểm giả định theo hành động với độ nhạy c ∈ {1,2,5}. Chẩn đoán đúng không đồng nghĩa đã tự xử lý; bộ SQL dùng c=2. Metric end-to-end/risk có mẫu số riêng.

4. **Demo** — đề xuất video 3 phút + `docker compose up` + Streamlit online chỉ hiển thị kết quả chạy sẵn (không gọi API).
5. **Protocol nhãn + UQ** — theo [RESEARCH_DECISIONS.md](RESEARCH_DECISIONS.md); H=90 cohort, observed-label/oracle riêng, agent abstention thực nghiệm, pilot 12/24/24. Các kết quả cũ cần chạy lại khi đổi protocol.

---

## 10. Tài liệu tham khảo

★ = đọc trong tuần 0.

Nguồn dùng để chốt protocol 25/10 đã đối chiếu trong [RESEARCH_DECISIONS.md](RESEARCH_DECISIONS.md). Danh sách rộng hơn dưới đây giữ từ v1, chưa được audit toàn bộ ở vòng research này.

**UQ cho LLM / agent**
- ★ Kirchhof, Kasneci, Kasneci — *Position: UQ Needs Reassessment for LLM Agents*, ICML 2025 — https://arxiv.org/abs/2505.22655
- Oh et al. — *Uncertainty Quantification in LLM Agents: Foundations, Emerging Challenges, and Opportunities*, ACL 2026 — https://preview.aclanthology.org/ingest-acl/2026.acl-long.738/
- Liu et al. — *UQ and Confidence Calibration in LLMs: A Survey*, KDD 2025 — https://arxiv.org/abs/2503.15850
- ★ Ren et al. — *Robots That Ask For Help (KnowNo)*, CoRL 2023 — https://proceedings.mlr.press/v229/ren23a.html
- *Towards Agents That Know When They Don't Know: Uncertainty as a Control Signal*, NeurIPS 2025 workshop — https://arxiv.org/abs/2509.02401
- *AgentAbstain* — https://www.alphaxiv.org/abs/2607.10059
- Angelopoulos & Bates — *A Gentle Introduction to Conformal Prediction* (2021) — https://arxiv.org/abs/2107.07511
- Geifman & El-Yaniv — *Selective Classification for Deep Neural Networks* (NeurIPS 2017) — https://arxiv.org/abs/1705.08500
- Angelopoulos et al. — *Learn then Test* — https://arxiv.org/abs/2110.01052
- Zadrozny — *Learning and Evaluating Classifiers under Sample Selection Bias* (ICML 2004) — https://research.ibm.com/publications/learning-and-evaluating-classifiers-under-sample-selection-bias

**Text-to-SQL có uncertainty**
- ★ Maleki et al. — *Confidence Estimation for Text-to-SQL in LLMs* (2025) — https://arxiv.org/abs/2508.14056
- TrustSQL — https://arxiv.org/abs/2403.15879
- Hasson & Guo — node-level uncertainty (2025) — https://arxiv.org/abs/2511.13984
- Semantic-entropy cho text-to-SQL (2026) — https://arxiv.org/abs/2602.12015

**Monitoring khi chưa có nhãn**
- ★ NannyML — CBPE — https://nannyml.readthedocs.io/en/main/how_it_works/performance_estimation.html
- *Estimating Model Performance Under Covariate Shift Without Labels* — https://arxiv.org/abs/2401.08348
- Rabanser et al. — *Failing Loudly* (NeurIPS 2019) — https://arxiv.org/abs/1810.11953

**Fraud detection**
- ★ Dal Pozzolo et al. — *Credit Card Fraud Detection: A Realistic Modeling and a Novel Learning Strategy*, IEEE TNNLS — https://boracchi.faculty.polimi.it/docs/2017_FraudsTNNLS.pdf
- ★ Le Borgne et al. — *Reproducible ML for Credit Card Fraud Detection — Practical Handbook* — https://github.com/Fraud-Detection-Handbook/fraud-detection-handbook

**Agent điều tra sự cố / data science agent**
- *LLM-Based Data Science Agents: A Survey* (2025) — https://arxiv.org/abs/2510.04023
- ARGUS — MCP-grounded RCA cho Kubernetes — https://arxiv.org/abs/2608.23084
- Yao et al. — *ReAct* (2022) — https://arxiv.org/abs/2210.03629
