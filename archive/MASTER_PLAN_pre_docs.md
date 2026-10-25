# PayGuard — Master Plan

> Lộ trình tổng, trạng thái hiện tại, và các điểm còn cần bàn.
> Thiết kế module: [PROJECT_PLAN.md](PROJECT_PLAN.md). Protocol nhãn và tự đóng: [RESEARCH_DECISIONS.md](RESEARCH_DECISIONS.md). Cập nhật: 25/10/2026.
>
> Mục 4 và protocol research là quyết định hiện hành; nhật ký 24/10 giữ lịch sử. Code simulator/model bản 24/10 đã rollback để bàn lại plan: các số P1/P2 dưới đây là của bản cũ, chỉ để tham khảo. Phạm vi MVP: mục 4-I.

---

## 1. Đích đến

**Business problem:** alert drift của model fraud quá nhiều, phần lớn vô hại; nhãn về chậm 30–90 ngày; điều tra mỗi alert tốn hàng giờ → alert fatigue.

**Project "xong" khi có đủ:**
1. Luồng end-to-end chạy bằng một lệnh: dữ liệu → model → monitoring → alert → rule triage → agent → báo cáo.
2. Bảng kết quả trả lời được: *hybrid rule + agent có UQ có hơn rule-based một mình không, ở nhóm sự cố nào, với chi phí bao nhiêu?*
3. Demo: video 3 phút + `docker compose up` + Streamlit online chỉ đọc kết quả.
4. README có problem statement, kiến trúc, kết quả, design decisions, limitations, related work.
5. Nhãn/feature đúng as-of; evaluator tính cả monitoring bỏ sót và rule đóng nhầm; mọi tỷ lệ lỗi có mẫu số và khoảng bất định phù hợp. Pilot không được diễn giải thành bảo đảm rủi ro gần 0.

---

## 2. Trạng thái

| Phase | Nội dung | Trạng thái |
|---|---|---|
| P0 | Research, định vị, thiết kế | ✅ Research có mục tiêu hoàn tất 25/10 — protocol nhãn + UQ MVP đã chốt |
| P1 | Simulator (dòng thời gian gốc 7/2025–6/2026, vai trò tài khoản, Tết, 4 kiểu fraud + cash-out, drift, nhãn trễ) | 🔁 Làm lại — bản 24/10 đã rollback (từng báo 878K giao dịch, 10/10 integrity check); số cũ chỉ để tham khảo |
| P1.5 | Incident injector + định nghĩa alert | 🔁 Làm lại — bản 24/10 đã rollback (từng báo 60 episode, 44 sự cố) |
| P2 | Fraud model + calibration + conformal | 🔁 Làm lại — bản 24/10 đã rollback (từng báo PR-AUC 0,92–0,93 trước drift, 0,48 sau drift; social engineering lọt 99%; tỉ lệ review không phản ứng với drift) |
| P3 | Monitoring + sinh alert | ⬜ |
| P4 | Rule triage → tool SQL có UQ → agent | ⬜ |
| P5 | Đánh giá + ablation | ⬜ |
| P6 | Đóng gói + trình bày | ⬜ |

**Việc đầu tiên:** đặt trần chi phí tạm cho pilot → viết lại P1 theo protocol 4-D và phạm vi MVP 4-I → luồng mỏng tới mốc ★ → đo token trên 3 case cố định → khóa ngân sách benchmark và số episode (4-F). Protocol khóa sau vòng sửa 25/10: không thêm vòng review trước khi có luồng mỏng chạy được. [16 ví dụ và kiểm tra protocol](review/verify_plan_claims.py) đã chạy, nhưng không kiểm tra implementation PayGuard.

---

## 3. Lộ trình chi tiết (~7 tuần còn lại)

Ước lượng theo ngày làm việc. ★ = mốc end-to-end bản thô. Chỉ làm phần MVP ở 4-I; phần "làm sau" không chặn mốc nào.

### P1 — Simulator (làm lại, 4 ngày)
- **Việc:** viết lại theo thiết kế đã có (vai trò tài khoản, Tết, 4 kiểu fraud + cash-out, drift) nhưng theo schema as-of; `business_calendar` có `expected_effect`, `lag_days`, `scope`; `event_at`/`available_at`, label event có `reported_at`/`available_at`, label tail ≥ H ngày; truth tách khỏi DB mà tool agent đọc.
- **Xong khi:** integrity check + leakage check đạt; truy vấn tại t không đổi khi thêm dữ liệu khả dụng sau t; snapshot nhãn ra đúng 4 trạng thái MVP.

### P1.5 — Incident injector & alert (3 ngày)
- **Việc:** module chèn sự cố vào dữ liệu nền (seasonal, pipeline bug đổi đơn vị, merchant mới mẫu nhỏ, fraud surge kiểu đã biết, fraud kiểu mới, kết hợp 2 nguyên nhân); 60 episode pilot có truth nguyên nhân/phạm vi/hành động; định nghĩa alert.
- **Đầu ra:** `simulator/incidents.py`, bảng truth sự cố (ngoài DB của agent) có `effect_available_at`, `accepted_actions`, `intervention_start_at`, `deadline_at`, tài liệu taxonomy nguyên nhân.
- **Xong khi:** mỗi episode có đúng nhãn nguyên nhân và nhóm known/unknown; validate.py kiểm tra được.

### P2 — Fraud model (5 ngày)
- **Việc:** snapshot nhãn với pending/matured-negative; chia fit/validation/calibration theo mục 4-D; feature đúng as-of; LightGBM; isotonic calibration; conformal giao dịch trên score thô; metric chi phí.
- **Đầu ra:** `model/`, bảng kết quả + reliability diagram. MLflow: mở rộng (4-I).
- **Xong khi:** audit as-of và split đạt; có logistic regression, observed-label/oracle evaluation tách riêng, coverage theo lớp trước/sau drift, false approval/decline/review. Undercoverage sau drift được báo cáo như kết quả, không chỉnh test để ép đạt 1−α.

### P3 — Monitoring & alert (4 ngày)
- **Việc:** job chạy theo ngày trên giai đoạn monitoring: PSI/KS feature, phân phối score & uncertainty, tỉ lệ cảnh báo, CBPE (NannyML); quy tắc phát alert; so ước lượng với performance thật khi nhãn về.
- **Đầu ra:** `monitoring/`, bảng `alerts`, dashboard Streamlit thô.
- **Xong khi:** mỗi episode sinh alert (hoặc không) tái lập được; ghép truth có trạng thái ambiguous/unmatched; detection và timely-routing recall đối soát được trên toàn bộ sự cố.

### ★ Mốc end-to-end thô (cuối tuần 3)
Rule triage tối thiểu + agent "1 bước" (1 câu SQL, không UQ) chạy được trên toàn bộ alert → ra một bảng kết quả, dù còn xấu.

### P4 — Triage (10 ngày)
- **P4a Rule triage (2 ngày):** 4 runbook MVP viết thành checklist code (RESEARCH 3.2a): lịch mùa vụ, đổi đơn vị/NULL, mẫu nhỏ, khoanh segment; rule đưa kết luận ứng viên qua gate chung, quyền tự đóng chọn theo từng runbook (RESEARCH 3.2b). Mẫu nhỏ là evidence insufficient; không tự đóng chỉ do p-value/lịch.
- **P4b Tool `run_sql` có UQ (4 ngày):** N=3 cố định trước, execution consistency, cache theo snapshot; bộ 100–150 câu SQL (40/30/15/15) và DB phản ví dụ. Adaptive N là ablation.
- **P4c Agent điều tra (4 ngày):** ReAct với tools đúng as-of; tách cause/evidence/action; K=3, score và gate theo protocol research; agent chỉ đề xuất nguyên nhân, `evidence_status` do checklist tính; tau agent chọn trên phần ca thực sự tới agent sau khi khóa quyền của rule. Conformal agent là nhánh bổ sung; conformal giao dịch vẫn ở P2.
- **Xong khi:** có hành vi cho thiếu bằng chứng, tool lỗi, hết ngân sách và recheck; chạy hết episode trong trần chi phí mục 4-F; lưu lỗi rule và agent riêng.

### P5 — Đánh giá (4 ngày)
- So sánh cùng monitoring: rule + human · cùng rule + agent không UQ · luôn chuyển người · cùng rule + agent có UQ. Agent so hai cách (RESEARCH 3.5): *tác dụng gate UQ* — cùng kết luận đa số và bằng chứng của 3 trace, chỉ bật/tắt điều kiện `close_score >= tau`; *giá trị vận hành* — agent một lượt chạy thật vs hệ thống đầy đủ, kèm chi phí.
- Metric end-to-end và rủi ro có mẫu số; utility + độ nhạy c ∈ {1, 2, 5}; tách unknown đối với rule và cơ chế chưa gặp; ablation với chi phí sampling công khai.
- **Xong khi:** có bảng và biểu đồ cuối cùng cho README, kể cả kết quả âm tính.

### P6 — Đóng gói (5 ngày)
- FastAPI, Streamlit, Docker Compose, pytest + CI, README, video, bản Streamlit online chỉ đọc, blog (tuỳ chọn).

### Nâng cấp sau (không chặn)
Kafka/Spark/Airflow · ensemble epistemic · user simulator · Next.js · user study nhỏ.

---

## 4. Quyết định hiện hành sau audit/research

Research có mục tiêu đã hoàn tất; phần implementation cần code P1/P2 để kiểm tra. [RESEARCH_DECISIONS.md](RESEARCH_DECISIONS.md) định nghĩa chính xác schema, score/gate, metric và giả định thống kê.

> **A–D cập nhật ngày 25/10/2026** (thứ tự phụ thuộc D → C → B → A). Tham số benchmark là lựa chọn thiết kế, không phải chuẩn ngành đã được xác minh.

### D. Nhãn trễ, target và chia dữ liệu ✅ Protocol chốt; chờ audit code
- Số cũ tại T0=1/2/2026: tháng 10/11/12/1 có 100%/89%/45%/29% nhãn *sẽ về* đã về. Các số này không tự chứng minh calibration sai/đúng và không đo fraud không bao giờ được báo.
- **Nhãn append-only, đúng as-of:** confirmed_fraud / confirmed_legit / matured_negative / pending / disputed; sửa nhãn không ghi đè lịch sử. Chỉ đọc sự kiện đã khả dụng. Pending không dùng như nhãn âm; disputed loại khỏi supervised fit và báo số lượng.
- **MVP H=90 ngày:** chọn cả cohort đủ tuổi cho supervised fit/calibration; unreported sau H là matured_negative theo policy. Feedback sớm chỉ thêm vào train trong ablation riêng. H=30/60 và H theo kênh là sensitivity, không phải chuẩn nghiệp vụ mặc định.
- **Timeline (rút gọn 25/10, 3 tập):** fit 1/7–21/9/2025; validation 22–30/9 (early stopping, chọn model); **calibration 10/2025** dùng chung cho isotonic, ngưỡng conformal và reference CBPE; T0=1/2/2026. Mọi cohort đã ≥ 92 ngày tại T0. Kiểm tra support trước khi dùng; thiếu mẫu thì mở rộng lịch sử.
  - Vì sao gộp được: conformal Mondrian tính trên **score thô** của LightGBM (predictor đã cố định trước tháng 10) nên bảo đảm coverage không phụ thuộc calibrator; isotonic là biến đổi đơn điệu, chỉ dùng để ra xác suất dễ đọc. CBPE tự calibration lại trên reference.
  - Hệ quả: reliability diagram **không** được vẽ trên tháng 10 (in-sample của calibrator) mà trên dữ liệu sau T0 khi nhãn về, hoặc truth ở evaluator.
- Reference drift PSI/KS: 11/2025–1/2026, không cần nhãn. Reference CBPE: tháng 10 có nhãn theo policy, predictor cố định.
- Báo calibration trên observed-label riêng với oracle fraud truth. Truth chỉ evaluator truy cập; oracle-label training là ablation. Sinh label-event tail ít nhất H ngày sau giao dịch cuối để đánh giá nhãn trễ, không đưa giao dịch tương lai vào feature.

### C. Nguyên nhân, bằng chứng và hành động ✅ Chốt
| Nhãn nguyên nhân | Ý nghĩa | Routing ứng viên | Phạm vi rule |
|---|---|---|---|
| `seasonal` | Lễ, khuyến mãi, ngày lương | Đề xuất benign; tự đóng phải qua gate | known nếu khớp runbook lịch |
| `legit_business_change` / `no_material_change` | Thay đổi hợp lệ / không có thay đổi trọng yếu có đủ bằng chứng | Đề xuất benign qua gate | Theo runbook đã viết |
| `data_pipeline` | Đổi đơn vị, NULL hàng loạt | Ticket cho đội data | known |
| `fraud_surge` | Kiểu gian lận đã có trong train tăng đột biến (card testing, ATO, collusion) | Báo fraud ops | known |
| `new_fraud` | Kiểu chưa từng thấy (social engineering) | Chuyển người + đề xuất retrain | unknown |
| `other` | Chưa xác định được nguyên nhân trong taxonomy | Chuyển người | Theo dữ liệu |

`insignificant` cũ được loại khỏi taxonomy; **mẫu nhỏ/thiếu bằng chứng là evidence_status=insufficient**, không phải truth benign. Output gồm cause_hypotheses, evidence_status và action riêng. Chưa đủ bằng chứng → recheck tối đa hai lần cách nhau một ngày, sau đó chuyển người; đang chờ không tính là đã giải quyết.

Rule và agent cùng qua gate: checklist runbook đạt (do code tính từ output tool, không để LLM tự điền), tool/snapshot hợp lệ, không điều kiện bắt buộc chuyển người nào bị kích hoạt, benign ứng viên, và người đề xuất có quyền tự đóng. **Quyền tự đóng tách riêng:** mỗi runbook on/off chọn trước; sau đó tau_agent ∈ {2/3,1} chọn trên phần ca tới agent. Một runbook đóng nhầm chỉ tắt runbook đó, không tắt agent. Score không tự là xác suất; K=N=3. Chi tiết: RESEARCH 3.2–3.2b. Tập conformal khác singleton → chuyển người trong nhánh thử nghiệm; singleton nguy hiểm vẫn routing.

**Utility theo hành động** (điểm giả định, chưa phải tiền tiết kiệm): benign đóng đúng +1; tự routing data_ticket/fraud_ops đúng đội +1; fraud chuyển nhầm data −3; đóng nhầm fraud −5; đóng nhầm pipeline −3; benign tạo ticket sai −1; human_review/recheck luôn 0. Ground truth lưu required_actions; nhiều nguyên nhân có thể cần nhiều routing và chẩn đoán được chấm riêng. Không gán mọi alert về nguyên nhân nghiêm trọng nhất chỉ do chồng thời gian.

Nhân mức phạt với 1,2,5; workload báo riêng số case/ticket/recheck và runtime. Chưa có user study thì số phút người tiết kiệm là giả định, không phải kết quả đo.

### B. Sinh sự cố — "thế giới song song" ✅ Chốt
- Giữ nguyên thế giới (khách hàng, merchant, hành vi, mạng người nhận) và lịch sử trước T0; **sinh lại 30 ngày sau T0** cho mỗi episode với seed riêng và 0–2 sự cố chèn vào.
- **60 episode pilot:** 20 clean + 40 injected, 4 episode hai nguyên nhân. Chia **12 development / 24 calibration / 24 test** ở cấp episode, với 4/8/8 clean; stratify cơ chế theo mức support. Nếu toàn bộ đã được dùng để thiết kế thì sinh calibration/test seed mới. Lưu manifest trước tuning.
- Development viết rule/prompt/score, chọn monitoring và matcher; calibration chỉ chọn quyền đóng từng runbook rồi tau agent (RESEARCH 3.2b); test policy đã khóa. `rule_covered` khác `mechanism_seen_in_calibration`; giữ stress test cơ chế mới riêng nếu muốn đo generalization.
- Ngày tháng trong episode là danh nghĩa: episode không có lịch cố định (không có Tết); sự kiện mùa vụ được chèn và ghi vào bảng **`business_calendar`** mà rule/agent được truy vấn — trừ biến thể "trend không có trong lịch".
- 60 episode chưa chứng minh rủi ro gần 0. Bootstrap theo episode cho metric incident; mẫu số 0 là NA. Nhánh chứng nhận mở rộng: policy cố định, loss=max(unsafe closure) mỗi episode nguy hiểm; 0/299 episode độc lập cùng phân phối cho cận trên một phía 95% <=1%. Không gọi cận theo episode là cận theo incident hay bảo đảm ngoài simulator.
- Dòng thời gian gốc (có Tết thật, drift 1/4) giữ lại làm **demo**.

### A. Định nghĩa alert ✅ Chốt
- 4 tầng **metric → signal → alert → incident**; job chạy mỗi ngày, cửa sổ 7 ngày so với tham chiếu.
- Metric gồm feature phía người nhận; cửa sổ cash-out 24h chỉ dùng khi đã quan sát đủ hoặc ghi rõ bị censored. Mọi truy vấn giới hạn event/availability time và episode. Agent không được truy cập truth/injector qua SQL hay file tool.
- Ngưỡng khởi đầu PSI > 0,25, KS p < 0,01; bản Benjamini–Hochberg làm baseline phụ; **chỉnh monitoring trên clean development**, khóa trước calibration/test.
- Alert cách nhau ≤ 2 ngày và trùng feature/segment với incident **đang mở** → gộp. Incident đã có quyết định cuối thì alert mới tạo **incident con** (`parent_incident_id`), không mở lại. **Chấm từng quyết định theo `decision_at`:** chỉ tính sự cố đã khả dụng tới lúc đó; sự cố phát sinh sau tính vào recall qua incident con, không phạt ngược quyết định cũ (RESEARCH 3.3a). Báo cáo kèm số alert.
- Ghép đáp án dùng thời gian (gồm hiệu ứng trễ cửa sổ), entity/segment và feature bị tác động. Giữ `matched` / `ambiguous_match` / `unmatched`; không khớp không tự thành benign. Matcher khóa trên development; mọi ca vẫn trong báo cáo số lượng/workload.
- Metric bắt buộc: detection recall và timely-routing recall trên **mọi sự cố cần can thiệp** (truth có `accepted_actions`, `intervention_start_at`, `deadline_at`; D = 7 ngày, sensitivity 3/14; recheck không tính là routing — RESEARCH 3.3); dangerous closure rate trên incident nguy hiểm tới triage; unsafe fraction trong số tự đóng; benign automation; nguồn lỗi rule/agent; chi phí cả recheck. Triage accuracy riêng chỉ tính ca có truth rõ và công khai ca không chấm được. Chưa đo phản hồi người thì routing đúng không đồng nghĩa đã xử lý thành công.

### E. Social engineering có "nhìn thấy" được không? ✅ Thiết kế chốt; simulator cần làm lại
> Bản simulator 24/10 (đã rollback) đã làm: vai trò tài khoản (cá nhân có người nhận quen / hộ kinh doanh nhận tiền qua tài khoản cá nhân / mule nội bộ chuyển tiền ra trong vài giờ), số tiền lừa đảo sát ngưỡng 10 triệu, sự cố bẫy `legit_business_spike`. Kết quả đo: hộ kinh doanh có fan-in (~50) **cao hơn** mule (9) → fan-in một mình vô dụng; tỉ lệ chuyển ra trong 24 h tách rõ (mule 1,0 vs ≤ 0,11). Còn lại cho P2: feature phía người nhận và nhãn điều tra viên theo alert của model.

Nội dung ban đầu:
Kiểu drift này dùng thiết bị quen, đúng tỉnh, ban ngày → nếu model chỉ có feature thiết bị/vị trí/thời gian thì **không có tín hiệu nào** để monitoring hay agent bắt được.
- **Đề xuất:** thêm feature phía người nhận: *lần đầu chuyển cho counterparty này*, *số người gửi khác nhau tới counterparty trong 7 ngày (fan-in)*, *tỉ trọng chuyển khoản của khách lớn tuổi*. Model train chưa thấy pattern → vẫn bỏ sót (đúng câu chuyện), nhưng monitoring có tín hiệu (fan-in của tài khoản nhận tăng) và agent có cái để điều tra.

### F. Ngân sách chi phí LLM 🟡 — chốt sau luồng mỏng
F quyết định số episode benchmark nhưng cần pipeline để đo, nên chia hai mức: trần tạm cho pilot (không chặn P1–P3) và ngân sách benchmark khóa sau khi đo. Lý do F quan trọng: simulator sinh dữ liệu gần như miễn phí, giới hạn thật là chi phí agent. 60 episode là **sàn**, không phải trần.
- Cấu hình MVP: K=N=3 cố định (theo mục C); adaptive N và thay K là ablation. Cache theo khóa ở PLAN_REVIEW bước 5; replay cache không tính là mẫu mới.
- Ước lượng thô mỗi incident tới agent: 3 lượt × ~5 câu SQL × 3 mẫu ≈ 45 lần sinh SQL + ~15–20 bước suy luận ≈ **60 lần gọi**. Chi phí ≈ số incident tới agent × 60 × token/lần × giá.
- **Việc:** (1) tra giá DeepSeek V4 Flash trên trang chính thức (logprobs không cần cho MVP) và đặt trần tạm cho pilot; (2) khi có luồng mỏng, chạy 3 incident cố định để đo token thật; (3) khóa trần USD cho toàn bộ benchmark (gồm cả 4 baseline và ablation) → suy ra số episode tối đa; nếu vượt 60 thì tăng cỡ calibration/test theo cùng tỉ lệ 1:2:2.

### G. Code và khả năng tái lập 🟡
- Code bản 24/10 đã rollback; P1/P2 viết lại từ đầu trong workspace này.
- Ngay khi bắt đầu code: `git init` trong workspace, version control, manifest seed/split/config/model; bỏ dữ liệu sinh lại và môi trường ảo khỏi tracking. Không giả định các số đo cũ đã được tái lập.

### H. Đồng bộ tài liệu ✅
- PROJECT M1–M4/đánh giá cập nhật theo protocol 25/10; lộ trình dùng mục 3 file này. Việc kiểm tra DeepSeek/API ở F vẫn chưa thuộc research hai chủ đề hiện tại.
- [PLAN_REVIEW.md](PLAN_REVIEW.md) là lịch sử kiểm chứng; [RESEARCH_DECISIONS.md](RESEARCH_DECISIONS.md) ghi quyết định MVP và điểm dừng research.

### I. Phạm vi MVP và phần làm sau ✅ Chốt 25/10
Protocol ở RESEARCH_DECISIONS là đích; MVP là phần tối thiểu để trả lời câu hỏi chính. Phần "làm sau" không sai, chỉ chưa làm thay đổi kết luận chính. Schema chừa sẵn cột để thêm sau không phải viết lại.

**MVP — bắt buộc trước khi báo kết quả**
| Nhóm | Nội dung | Lý do |
|---|---|---|
| Nhãn | Snapshot 4 trạng thái confirmed_fraud / confirmed_legit / matured_negative / pending; H=90 cố định; mỗi giao dịch tối đa một kết luận | Pending không bị âm hóa (R1) |
| As-of | `event_at`/`available_at`; mọi feature/SQL giới hạn theo `as_of_time` và episode | Tránh nhìn tương lai (R6) |
| Truth | Truth/injector ở file/DB riêng, tool agent không có kết nối tới | Kết quả vô giá trị nếu lộ (R6) |
| Split | Model 3 tập (4-D); episode dev/calibration/test + manifest khóa trước tuning | Không tune trên test |
| Quyết định | cause / evidence_status / action tách riêng; checklist 4 runbook bằng code; gate chung nhưng quyền tự đóng rule/agent chọn riêng; recheck ≤ 2 lần rồi chuyển người | Mẫu nhỏ ≠ vô hại (R2); một runbook lỗi không tắt agent |
| Incident theo thời gian | Gộp chỉ vào incident đang mở; đã quyết định thì tạo incident con; chấm từng quyết định theo `decision_at` | Không phạt ngược quyết định đúng |
| UQ | Agent: K=N=3, tau ∈ {2/3, 1}; giao dịch: Mondrian conformal | Câu hỏi UQ của MVP: *bắt đồng thuận tuyệt đối có giảm đóng nhầm không?* |
| Đánh giá | Detection recall, timely-routing recall, dangerous closure rate, unsafe fraction, benign automation, workload, chi phí; bootstrap theo episode; mẫu số 0 = NA | Metric end-to-end (R5) |
| Attribution | matched / ambiguous_match / unmatched tự động; ambiguous + unmatched được đếm và báo cáo, không chấm accuracy | Không gán bừa benign (R7) |
| Baseline | rule + người · rule + agent không UQ · rule + agent có UQ · luôn chuyển người, cùng monitoring; agent không UQ = cùng kết luận đa số + bằng chứng, bỏ điều kiện score (RESEARCH 3.5) | Câu hỏi chính; chênh lệch không đến từ lượng tính toán |
| SQL | Bộ câu hỏi 40/30/15/15 + vài DB phản ví dụ cho lỗi mẫu số / join / NULL / pending | Consistency ≠ đúng (R9) |

**Làm sau — không chặn mốc nào**
- Nhãn: trạng thái `disputed`, sửa nhãn append-only (`supersedes_event_id`), H theo kênh, sensitivity H=30/60.
- Ablation dữ liệu: oracle-label training, đưa feedback sớm vào train.
- UQ: conformal agent kiểu KnowNo, adaptive N / thay K, LTT.
- Thống kê: nhánh chứng nhận (299 episode nguy hiểm, 0 lỗi), stress test cơ chế chưa gặp.
- Đánh giá: phân xử thủ công ca `ambiguous_match`, bộ DB phản ví dụ đầy đủ, MLflow.

**Trình bày:** README/phỏng vấn dùng bản tóm tắt dễ đọc; các file protocol giữ cho chi tiết. Câu CV đổi "độ tin cậy được hiệu chỉnh" thành "ngưỡng đồng thuận chọn trên tập calibration".

---

## 5. Nhật ký quyết định

| Ngày | Quyết định |
|---|---|
| 25/10/2026 | Sửa sau review lần 2: runbook mùa vụ kiểm tra vị trí + độ lớn theo ngày thay cho độ phủ ≥ 50%; hai phép so sánh agent (gate UQ: cùng kết luận + bằng chứng, chỉ bật/tắt điều kiện score / giá trị vận hành K=1 vs K=3); timely routing có `accepted_actions` + `deadline_at` (D = 7), recheck không tính routing |
| 25/10/2026 | Sửa sau review: tách quyền tự đóng rule (theo runbook) và agent (tau); incident con + chấm theo `decision_at`; checklist runbook bằng code; ngân sách tạm → đo trên luồng mỏng → khóa; gắn nhãn MVP/mở rộng tại từng module |
| 25/10/2026 | Code P1/P2 bản 24/10 rollback → làm lại. Khóa protocol, chốt phạm vi MVP/làm sau (4-I). Rút split model từ 4 xuống 3 tập: fit 1/7–21/9, validation 22–30/9, calibration tháng 10 dùng chung cho isotonic + conformal (score thô) + CBPE. F (ngân sách) quyết định số episode, 60 là sàn |
| 25/10/2026 | Chốt protocol nhãn H=90 theo cohort, pending không âm hóa, tách observed-label/oracle và các vai trò calibration; timeline cũ cần chạy lại |
| 25/10/2026 | UQ agent MVP dùng abstention thực nghiệm + gate chung rule/agent; conformal agent chuyển thành nhánh bổ sung; conformal giao dịch giữ ở P2 |
| 25/10/2026 | Pilot 12/24/24 development/calibration/test; thêm metric end-to-end, attribution ambiguous/unmatched và rủi ro theo mẫu số; chưa có chứng nhận gần 0 |
| 24/10/2026 | Định vị theo business problem (alert fatigue), không theo tính mới |
| 24/10/2026 | Triage hybrid: rule trước, agent sau; rule-based là baseline chính |
| 24/10/2026 | SQL 40/30/15/15 · user simulator để sau · ma trận chi phí (−5/−3/−1/0/+1) + độ nhạy · demo video + Docker + Streamlit chỉ đọc |
| 24/10/2026 | LLM: DeepSeek V4 Flash; UQ black-box trước |
| 24/10/2026 | Simulator v0: collusion chỉ trước mốc drift để giai đoạn drift có một nguyên nhân |
| 24/10/2026 | Simulator: bản đầy đủ vai trò tài khoản (hộ kinh doanh, mule nội bộ có cash-out), thêm sự cố bẫy `legit_business_spike` |
| 24/10/2026 | Chốt A–D: chia dữ liệu theo độ chín của nhãn (T0 = 1/2/2026); taxonomy 5 nhãn + ma trận chi phí mở rộng; 60 episode "thế giới song song" + `business_calendar`; alert 4 tầng, chấm theo incident |
| 24/10/2026 | P2: conformal Mondrian **bất đối xứng** (α_legit 0,005, α_fraud 0,20) vì α bằng nhau chặn ~10% giao dịch hợp lệ và α_fraud 0,10 đẩy ~9% giao dịch vào review; huấn luyện tất định để kết quả tái lập được |
| 24/10/2026 | Audit nghiệp vụ simulator, làm mức 1+2: thiết bị hợp lệ thay đổi, chữ ký ATO/card testing theo xác suất, nhãn theo kênh (chargeback chỉ thẻ) + friendly fraud, Tết đổi cơ cấu (lì xì), mule thuê, hộ bán online trả nhà cung cấp, refund/reversal tách dòng, merchant quen, retry; thêm leakage check (max AUC artefact 0,91 < 0,97). Độ chín nhãn đo lại tại T0: T10 100%, T11 89%, T12 45%, T1 29% → mốc chia D vẫn đúng |
