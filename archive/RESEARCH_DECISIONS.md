# PayGuard — Quyết định sau research có mục tiêu

> **Lưu trữ (25/10/2026):** nội dung đã chuyển sang `docs/` theo module; file này không còn được cập nhật. Liên kết bên trong trỏ tới vị trí cũ.

Ngày: 25/10/2026. Phạm vi: nhãn trễ/calibration và rủi ro tự đóng incident. Các quyết định MVP dưới đây được áp dụng trong MASTER_PLAN.md và PROJECT_PLAN.md. PLAN_REVIEW.md giữ lại lập luận và phản ví dụ của vòng review trước.

## 1. Nghiên cứu giải quyết được gì?

| Câu hỏi | Điều đã xác minh | Quyết định cho PayGuard |
|---|---|---|
| Chưa bị báo fraud có phải hợp lệ? | Feedback điều tra viên và nhãn trễ có cơ chế chọn mẫu khác nhau; giao dịch chưa bị tranh chấp có thể được coi hợp lệ sau thời gian chờ, nhưng đó là một chính sách gán nhãn. [Dal Pozzolo, II-C và V](https://boracchi.faculty.polimi.it/docs/2017_FraudsTNNLS.pdf) | Lưu trạng thái và nguồn; calibration chính dùng cohort đủ tuổi theo chính sách định trước. |
| Có tự sửa selection bias bằng cách bỏ pending? | Chọn tập có nhãn có thể làm thay đổi phân phối cần đánh giá. [Zadrozny, ICML 2004](https://research.ibm.com/publications/learning-and-evaluating-classifiers-under-sample-selection-bias) | Chọn nguyên cohort theo tuổi giao dịch; tách feedback sớm thành ablation. |
| Singleton conformal có bảo đảm? | KnowNo có bảo đảm dưới phân phối tình huống và cơ chế human help được giả định; hỏi người khi tập không phải singleton. [KnowNo, 3.2](https://proceedings.mlr.press/v229/ren23a/ren23a.pdf) | Không dùng bảo đảm task completion thay cho tỷ lệ đóng nhầm có mẫu số khác. |
| Muốn kiểm soát lỗi trong nhóm được tự xử lý? | Selective risk được định nghĩa trên phần được chọn; đánh đổi với coverage. [Geifman & El-Yaniv, mục 2](https://papers.nips.cc/paper_files/paper/2017/file/4a8423d5e91fda00bb7e46540e2b0cf1-Paper.pdf) | Báo cáo riêng rủi ro trên incident nguy hiểm và rủi ro trong nhóm tự đóng. |
| Chọn threshold rồi tính CI trên cùng tập có đủ không? | Learn then Test xử lý việc chọn policy bằng kiểm định với kiểm soát nhiều giả thuyết; mục 3.2 minh họa selective accuracy. [LTT, 2.3 và 3.2](https://arxiv.org/html/2110.01052v5) | Pilot tune trên calibration, đánh giá trên test giữ riêng. Bảo đảm chính thức là thí nghiệm bổ sung. |

Các tham số H=90 ngày, K=N=3, cách chia 12/24/24 và ngưỡng rủi ro 1% dưới đây là **lựa chọn thiết kế benchmark**, không phải chuẩn nghiệp vụ suy ra từ các nguồn trên.

## 2. Hợp đồng nhãn và mục tiêu dự đoán

### 2.1 Snapshot nhãn tại thời điểm t

Lưu sự kiện nhãn append-only: `txn_id`, `event_id`, `label`, `label_status`, `reported_at`, `available_at`, `source`, `supersedes_event_id`. Sự kiện sửa nhãn không được ghi đè lịch sử. `available_at >= reported_at`; tool chỉ thấy sự kiện có `available_at <= t`. Trường hợp hai nguồn kết luận mâu thuẫn mà chưa có adjudication được đánh dấu `disputed`, không tự chọn nguồn hoặc ưu tiên fraud một cách ngầm định.

| Trạng thái snapshot | Điều kiện | Dùng như nhãn giám sát? |
|---|---|---|
| `confirmed_fraud` | Kết luận fraud đã khả dụng | 1, theo protocol cohort bên dưới |
| `confirmed_legit` | Kết luận hợp lệ đã khả dụng | 0, theo protocol cohort |
| `matured_negative` | Tuổi >= H, chưa có kết luận khả dụng, không disputed | 0 theo chính sách; không gọi là chân lý fraud |
| `pending` | Tuổi < H, chưa có kết luận | Không dùng làm 0 |
| `disputed` | Có kết luận mâu thuẫn chưa phân xử | Loại khỏi supervised fit, báo số lượng và lý do |

**Protocol chính:** H=90 ngày cho mọi kênh để MVP đơn giản; chỉ lấy các giao dịch có tuổi >= H vào tập fit/calibration có nhãn. Bao gồm cả positive và negative của cohort đó, thay vì chọn positive sớm rồi chỉ chờ negative. Feedback sớm vẫn hiển thị cho điều tra khi khả dụng, nhưng việc đưa cohort non trẻ vào train là ablation riêng. H theo kênh và H=30/60 là sensitivity experiment, không chọn H sau khi xem test.

Nhãn quan sát có thể tiếp tục được sửa khi báo cáo đến muộn. Mọi báo cáo ghi `as_of_time`, `label_policy_version` và `model_version`.

> **MVP:** chỉ 4 trạng thái `confirmed_fraud` / `confirmed_legit` / `matured_negative` / `pending`; mỗi giao dịch tối đa một sự kiện kết luận. **Mở rộng:** `disputed`, sửa nhãn qua `supersedes_event_id`, H theo kênh, H=30/60. Schema giữ sẵn các cột này (để NULL) để thêm sau không phải viết lại. Xem MASTER 4-I.

### 2.2 Định nghĩa target để tránh hứa quá mức

- **Target vận hành:** trạng thái fraud theo policy gán nhãn tại snapshot đánh giá. Calibration/ECE/reliability diagram trên target này được ghi là `observed-label calibration`.
- **Truth simulator:** có gian lận thực sự hay không; chỉ evaluator đọc được. Đo thêm PR-AUC, calibration, missed fraud và lượng fraud bị `matured_negative` gán âm sai trên truth. Các nhãn friendly fraud/chargeback không tự đồng nhất với truth hành vi.
- Thí nghiệm dùng truth để fit/calibration là `oracle-label ablation`, không thuộc luồng vận hành.
- Báo tỷ lệ fraud không bao giờ được báo cáo và fraud về sau H bằng truth ở evaluator. Đủ 100% nhãn *sẽ về* vẫn có thể thiếu fraud không bao giờ được báo.

### 2.3 Timeline MVP ứng viên tại T0 = 01/02/2026

| Vai trò | Khoảng giao dịch (đầu gồm, cuối không gồm) |
|---|---|
| Fit model | [01/07/2025, 22/09/2025) |
| Validation (early stopping, chọn model/threshold giao dịch) | [22/09/2025, 01/10/2025) |
| Calibration: fit isotonic + ngưỡng conformal trên score thô + reference CBPE | [01/10/2025, 01/11/2025) |
| Reference drift PSI/KS | [01/11/2025, 01/02/2026), không đòi nhãn |
| Monitoring/test episode | 30 ngày sau T0, từng episode có ID riêng |

Rút gọn ngày 25/10 từ 4 xuống 3 tập. Gộp isotonic và conformal vào cùng tháng 10 hợp lệ vì conformal Mondrian tính trên score thô của predictor đã cố định trước tháng 10, nên coverage không phụ thuộc calibrator; isotonic là biến đổi đơn điệu chỉ để ra xác suất dễ đọc. Hệ quả: reliability diagram/ECE không tính trên tháng 10 (in-sample của calibrator) mà trên dữ liệu sau T0 khi nhãn về, hoặc truth ở evaluator. Cần audit số lượng và số positive từng tập trước khi chạy. Tất cả cohort supervised trên đã >=90 ngày tại T0. Nếu không đủ support cho calibration theo lớp, mở rộng lịch sử về trước hoặc giảm scope. Kết quả P2 theo train 7–10/calibration 11 là run lịch sử, không được gắn vào protocol mới.

Đánh giá nhãn của giai đoạn monitoring cần thêm label-event tail tới ít nhất `last_transaction_at + H`; không tạo thêm giao dịch tương lai để lén bổ sung feature. Những báo cáo muộn hơn horizon vẫn được tính trong audit oracle; số liệu theo snapshot ghi rõ censoring. Khoảng thời gian quan sát giao dịch và quan sát nhãn là hai cấu hình riêng.

## 3. Hợp đồng quyết định và thí nghiệm UQ của agent

### 3.1 Target và đầu ra

`requires_intervention` là truth cần hành động: fraud hoặc lỗi pipeline có tác động theo cấu hình incident. Benign là thay đổi hợp lệ hoặc không có thay đổi trọng yếu. Mẫu nhỏ, tool lỗi và chưa đủ bằng chứng là trạng thái nhận thức, không phải một lớp truth benign.

```text
cause_hypotheses: tập giả thuyết + bằng chứng, không đồng nhất với nhiều nguyên nhân thật
evidence_status: sufficient | insufficient | conflicting
action: close | recheck | data_ticket | fraud_ops | human_review
close_score: score thực nghiệm, không mặc định là xác suất đúng
as_of_time, policy_version, tool_trace_ids
```

### 3.2 Gate chung cho rule và agent

Rule đưa ra kết luận ứng viên; các trường hợp đề xuất đóng cũng đi qua gate. Mùa vụ trên lịch, p-value lớn, hoặc thiếu dữ liệu không tự đủ để qua gate.

1. **Checklist runbook đạt** (mục 3.2a): `evidence_status` do code tính từ output của tool, LLM không tự điền. Agent chỉ đề xuất nguyên nhân và chỉ ra runbook muốn áp dụng; checklist chạy lại trên snapshot của incident.
2. Chẩn đoán ứng viên hỗ trợ benign; không điều kiện bắt buộc chuyển người nào của runbook bị kích hoạt.
3. Người đề xuất có quyền tự đóng: rule → runbook đó đang `close_enabled`; agent → `close_score >= tau_agent`. Hai quyền chọn riêng (mục 3.2b), khóa trước test.
4. Không đạt → `recheck` khi có thể chờ dữ liệu, hoặc `human_review`. Tối đa hai lần recheck, cách nhau một ngày; tới hạn vẫn thiếu bằng chứng thì chuyển người. Case chờ vẫn nằm trong workload và unresolved.

Mỗi lần gate chạy lưu: kết quả từng mục checklist kèm `tool_trace_ids`, kết luận và lý do của agent/rule, quyết định gate.

### 3.2a Checklist runbook MVP

Mỗi runbook là code, không phải prompt. "Bắt buộc" sai → `insufficient`; "chuyển người" đúng → `human_review` bất kể score.

| Runbook | Dữ liệu/kiểm tra bắt buộc | Cửa sổ phải hoàn tất | Bắt buộc chuyển người khi |
|---|---|---|---|
| `seasonal_calendar` → benign | Sự kiện trong `business_calendar` có **ngày ảnh hưởng** (ngày sự kiện + `lag_days` khai báo) giao với cửa sổ alert; (a) **vị trí:** độ lệch theo ngày tập trung vào ngày ảnh hưởng, các ngày còn lại trong cửa sổ trong dải tham chiếu; (b) **độ lớn:** độ lệch cửa sổ nằm trong `expected_effect × số ngày ảnh hưởng / 7` ± sai số; (c) hướng thay đổi khớp và segment bị lệch nằm trong `scope` của sự kiện | Cửa sổ 7 ngày đã đóng tại `as_of_time` | Bất kỳ metric phía fraud nào (tỉ lệ chuyển ra 24 h, người gửi lần đầu, số tiền sát 10 triệu, tỉ lệ review của model) cũng có signal trong cùng segment |
| `unit_or_null_change` → `data_ticket` | Tỉ số trung vị/giá trị sau ÷ trước thuộc {≈100, ≈1/100, ≈1000} ± 5%, hoặc tỉ lệ NULL tăng ≥ 20 điểm %; thay đổi đột ngột trong 1 ngày | Ít nhất 1 ngày sau điểm đổi | Cùng lúc có signal fraud trong segment đó |
| `small_sample` → `insufficient` | Số giao dịch trong segment < n_min (chốt trên development) | — | Hết 2 lần recheck vẫn < n_min |
| `segment_concentration` → chỉ khoanh vùng | Một segment chiếm ≥ 70% đóng góp PSI | Cửa sổ đã đóng | Không tự quyết định hành động; chỉ thu hẹp phạm vi cho runbook khác/agent |

Không dùng ngưỡng độ phủ cửa sổ cho mùa vụ: sự kiện 1 ngày chỉ phủ tối đa 1/7 ≈ 14,3% cửa sổ 7 ngày, nên điều kiện kiểu "phủ ≥ 50%" loại mọi ngày lương, 11/11 và khuyến mãi một ngày. Kiểm tra vị trí theo ngày còn bắt được fraud chen giữa đợt mùa vụ. Ngưỡng (dải tham chiếu theo ngày, sai số độ lớn, 5%, 20 điểm, n_min, 70%) là giá trị khởi đầu, chỉnh trên development rồi khóa. Agent đề xuất `benign` mà không khớp runbook benign nào → không được tự đóng trong MVP (`human_review`); đây là giới hạn có chủ ý.

### 3.2b Chọn quyền tự đóng: rule và agent tách riêng

Nếu rule và agent dùng chung một tau, rule có score cố định 1 nên một runbook đóng nhầm sẽ loại mọi tau và đưa cả hệ thống về `close_disabled`, kể cả khi agent phân biệt đúng (phản ví dụ `separated_rule_agent_authority` trong `review/`). Vì vậy chọn theo hai bước, lưới khai báo trước, cùng tập calibration:

1. **Rule:** mỗi runbook benign có `close_enabled ∈ {on, off}`. On khi trên calibration không có unsafe closure do runbook đó và có ít nhất một benign đóng đúng; ngược lại off. Ca khớp runbook off chuyển cho agent (rule kết luận làm ngữ cảnh), không mặc định chuyển người.
2. **Agent:** với cấu hình rule đã khóa ở bước 1, chọn `tau_agent ∈ {2/3, 1}` trên phần ca thực sự tới agent: tối đa benign đóng đúng với không unsafe closure quan sát; hòa lấy tau lớn hơn; không có tau hữu ích → `agent_close_disabled` (agent vẫn routing data/fraud/human).

Score agent ban đầu: K=3 lần điều tra, mỗi SQL lấy N=3 mẫu; `s = min(v_benign, min_sql_consistency)` trên toàn trace liên quan. Lượt không hoàn tất tính là phiếu không hỗ trợ benign; SQL lỗi tính consistency 0. `v_benign` là số lượt kết luận benign / 3. Nếu không dùng SQL thì bỏ thành phần SQL, nhưng vẫn phải có bằng chứng từ các tool cấu trúc. Score chỉ để xếp thứ tự, chưa phải xác suất. Adaptive N và thay K là ablation, cần đánh giá lại nguyên policy.

Rule không có score liên tục: khớp checklist đầy đủ hay không, quyền đóng theo bước 1 của 3.2b. Khớp runbook không tạo ra bảo đảm thống kê. Phải báo lỗi tự đóng riêng theo nguồn rule/agent và theo từng runbook.

Cả hai bước là tuning thực nghiệm trên vài chục ca; không gọi điều kiện zero-error trên calibration là bảo đảm rủi ro. Test giữ riêng đo cấu hình đã chọn. Bất kỳ sửa runbook/prompt/sampling/model sau đó đều tạo policy version mới.

### 3.3 Metric chính và đơn vị thống kê

**Metric hành vi chính:** dangerous closure rate = số incident cần can thiệp bị đóng / số incident cần can thiệp tới triage. Metric phụ: unsafe fraction among closures = số đóng sai / tổng số đã đóng. Cùng báo cáo end-to-end timely routing recall trên toàn bộ sự cố cần can thiệp, gồm cả monitoring bỏ sót. Mẫu số bằng 0 → `NA`, không báo 0% rủi ro.

**Timely routing — định nghĩa thực thi.** Truth mỗi sự cố cần can thiệp có `accepted_actions`, `intervention_start_at` (= `effect_available_at`) và `deadline_at = intervention_start_at + D`. Sự cố được tính routing đúng hạn khi có ít nhất một quyết định trên incident ghép với nó (kể cả incident con) có action ∈ `accepted_actions` và `intervention_start_at <= decision_at <= deadline_at`. Quyết định trên incident không ghép với sự cố, hoặc trước `intervention_start_at`, không được tính. `close` không bao giờ đạt; `recheck` là chưa giải quyết, không phải routing.

| Sự cố | `accepted_actions` | Ghi chú |
|---|---|---|
| `fraud_surge` | `fraud_ops`, `human_review` | |
| `new_fraud` | `human_review`, `fraud_ops` | Đề xuất retrain ghi kèm, không chấm |
| `data_pipeline` | `data_ticket`, `human_review` | |
| Hai nguyên nhân | Mỗi sự cố thành phần chấm riêng theo bảng trên | `human_review` thỏa cả hai |

Báo cáo tách *routing tự động đúng đội* (`fraud_ops`/`data_ticket`) khỏi *routing an toàn* (`human_review`). D là giả định nghiệp vụ khai báo trước, không tune: MVP D = 7 ngày (cửa sổ monitoring 7 ngày khiến alert cần vài ngày mới bật), sensitivity D ∈ {3, 7, 14}.

Nhiều incident trong một episode chia sẻ dữ liệu, nên không coi chúng là phép thử độc lập để lấy exact binomial bound. Báo CI cho metric theo incident bằng bootstrap theo episode, kèm giới hạn khi ít nhóm/zero errors.

**Nếu muốn có thí nghiệm chứng nhận mở rộng:** định nghĩa `L_episode = 1` nếu có ít nhất một incident cần can thiệp bị đóng nhầm trong episode nguy hiểm, ngược lại 0. Đây là rủi ro theo episode, không đổi tên thành rủi ro theo incident. Với policy cố định và episode mới độc lập cùng phân phối có điều kiện theo thế giới nền, dùng cận exact binomial một phía 95%. Mục tiêu nghiên cứu đặt trước 1%; 0 lỗi cần 299 episode nguy hiểm. Điều đó không chứng nhận thế giới thật hay unseen-mechanism stress test. [Cách tính exact binomial](https://itl.nist.gov/div898/software/dataplot/refman2/auxillar/exacbino.htm).

Không có alert không tính là đóng nhầm nhưng phải tính bỏ sót trong detection/routing recall. Không ghép được đáp án giữ trạng thái riêng; một unsafe closure đã xảy ra không bị xóa khi case được mở lại. Có closure chưa phân xử được truth thì báo số lượng và phạm vi chưa đánh giá; chưa được dùng tập loại các ca đó để tuyên bố chứng nhận rủi ro. Nhánh chứng nhận cần đáp án đầy đủ hoặc một loss bảo thủ định trước tính closure chưa phân xử là lỗi.

Case có hạn recheck vượt cuối episode được ghi censored/unresolved, không tự coi là đã xử lý thành công. Label-event tail không tạo thêm dữ liệu hành vi để hoàn tất giả cửa sổ feature.

### 3.3a Incident theo thời gian và chấm theo thời điểm quyết định

Nếu gộp cả chuỗi alert thành một incident rồi chấm bằng nhãn cuối, một quyết định đóng đúng ở ngày 1 (mùa vụ) có thể bị phạt vì fraud chỉ bắt đầu ngày 4 (phản ví dụ `decision_time_scoring` trong `review/`).

- **Gộp:** alert mới trùng feature/segment, cách ≤ 2 ngày với một incident **đang mở** (chưa có quyết định cuối, hoặc đang recheck) → gộp vào incident đó.
- **Không mở lại:** incident đã có quyết định cuối (close / data_ticket / fraud_ops / human_review) mà có alert mới → tạo incident mới có `parent_incident_id`. Quyết định cũ không bị sửa.
- **Lưu từng quyết định:** `decision_id`, `incident_id`, `decision_at`, `as_of_time`, action, phạm vi (feature/segment/khoảng thời gian), id các tool trace và kết quả checklist. Recheck cũng là một quyết định.
- **Evaluator:** truth của một quyết định = các sự cố chèn giao với phạm vi của nó và có `effect_available_at <= decision_at`, trong đó `effect_available_at` là `available_at` của giao dịch chèn đầu tiên (quy ước bảo thủ: đã khả dụng là tính, kể cả khi chưa đủ mạnh để kích hoạt signal). Sự cố bắt đầu sau đó không phạt quyết định cũ; nó được tính vào detection/routing recall qua incident con. Không có incident con → monitoring bỏ sót.
- Một unsafe closure đã xảy ra vẫn giữ nguyên dù incident con sau đó được routing đúng.

### 3.4 Pilot, bảo đảm và nhánh conformal

- 60 episode hiện có là pilot: 12 development (4 clean, 8 injected), 24 calibration (8 clean, 16 injected), 24 test (8 clean, 16 injected). Phân bổ mechanism/known/unknown và các episode hai nguyên nhân ở cấp episode; lưu manifest trước tuning. Nếu code đã dùng hết 60 để thiết kế/tuning, chúng thuộc development; sinh calibration/test seed mới.
- Tune monitoring, runbook, prompt, tool và score trên development. Calibration chỉ chọn quyền đóng từng runbook rồi tau agent (3.2b); không tiếp tục viết rule theo đáp án calibration. Test khóa một lần để báo cáo.
- UQ agent MVP = abstention theo score + risk–coverage thực nghiệm. Conformal giao dịch vẫn ở M2. Conformal agent/KnowNo là nhánh bổ sung khi có đủ calibration đúng routing; tập không phải singleton chuyển người. Chẩn đoán singleton nguy hiểm vẫn routing, không tự đóng.
- Nếu cần chọn nhiều policy trên cùng tập chứng nhận, phải dùng correction/quy trình hợp lệ như LTT hoặc một tập chứng nhận mới sau khi khóa policy. Không gắn CI của tập tuning lên policy được chọn rồi gọi là bảo đảm.
- Bootstrap, ECE và số lần sampling không thay thế được các giả định lấy mẫu. Không huấn luyện thêm model confidence nhiều tham số từ vài chục incident.

### 3.5 Hai phép so sánh agent (khóa trước benchmark, không chặn P1)

Nếu nhánh không UQ chạy một lượt còn nhánh UQ chạy nhiều lượt, chênh lệch có thể đến từ lượng tính toán và bằng chứng thay vì từ UQ. Vì vậy tách hai câu hỏi:

| So sánh | Nhánh A | Nhánh B | Trả lời |
|---|---|---|---|
| **Tác dụng của gate UQ** | Không gate: cùng kết luận ứng viên và cùng bằng chứng tổng hợp với B; hành động theo kết luận ứng viên, **bỏ** điều kiện `close_score >= tau_agent` | Có gate: giữ điều kiện `close_score >= tau_agent` | Riêng điều kiện score có giảm đóng nhầm không, khi kết luận và bằng chứng giữ nguyên |
| **Giá trị vận hành** | Agent một lượt chạy thật, K=N=1 | Hệ thống đầy đủ K=N=3 | Chất lượng tăng thêm có đáng chi phí không; báo cả chất lượng và USD/token |

- **Kết luận ứng viên** (dùng chung cho A và B ở so sánh 1): nguyên nhân chiếm đa số trong K=3 lượt; không có đa số (ví dụ 3 nguyên nhân khác nhau) → không phải benign. **Bằng chứng tổng hợp:** checklist runbook chạy trên snapshot của incident, kèm mọi tool trace của 3 lượt. Majority vote là cách tổng hợp dự đoán, không phải gate.
- Cả hai nhánh qua **cùng checklist runbook**, cùng quyền tự đóng của rule, cùng monitoring và snapshot; chỉ khác điều kiện score (so sánh 1) hoặc ngân sách tính toán (so sánh 2).
- Ghi chú diễn giải: đa số benign tương đương `v_benign >= 2/3`. Vì vậy khi `tau_agent = 2/3`, A và B chỉ khác nhau qua thành phần SQL consistency của `close_score`; khi `tau_agent = 1`, B còn đòi đồng thuận tuyệt đối. Nếu tau chọn ra là 2/3 và thành phần SQL không bao giờ chặn, A ≡ B — đó là kết quả cần báo cáo, không phải lỗi thí nghiệm.
- So sánh 1 không tốn thêm lời gọi LLM. So sánh 2 chạy thêm một lượt thật trên mọi ca tới agent; tính vào ngân sách 4-F.

## 4. Việc tiếp theo và điểm dừng research

1. Tích hợp code simulator/model khi có trong workspace; lập label/split manifest và chạy lại P2 theo protocol mới. Chưa có code thì trạng thái là chờ xác minh implementation, không tự suy diễn kết quả.
2. Làm luồng mỏng: snapshot → score cố định → một monitor → một rule → gate → case report; log `recheck`, routing và workload. Agent một bước được thêm sau khi evaluator chạy được; chạy 3 case cố định dưới trần chi phí tạm để đo token, rồi mới khóa ngân sách benchmark và số episode (MASTER 4-F).
3. Chạy failure cases: nhãn trễ hơn H, sửa nhãn sau T0, callback mất nhãn, fraud+mùa vụ, mẫu nhỏ, cửa sổ 24h chưa hoàn tất, multi-incident, SQL đồng thuận sai và không có alert.
4. So rule+human, cùng rule+agent không UQ, cùng rule+agent có UQ, luôn chuyển người; cùng monitoring và công khai chi phí sampling. Hai phép so sánh agent theo mục 3.5.

Research cho MVP dừng ở đây. Chỉ mở thêm khi label audit không hỗ trợ protocol đã chọn, confidence không phân tách đúng/sai, hoặc cần chứng minh loại rủi ro khác. Các kiểm tra toán học nhỏ trong `review/` xác nhận phép tính và tính nhất quán của protocol, không thay cho việc chạy PayGuard.
