# Data — simulator, nhãn, episode và truth

> Module này **sở hữu mọi schema dữ liệu**: giao dịch, nhãn, `business_calendar`, episode và bảng truth sự cố. Module khác chỉ link tới đây, không chép lại.
> Stage xây: P1 (simulator + injector). Lý do và nguồn: [rationale.md](rationale.md).

## 1. Simulator nền

- **Nền:** ý tưởng simulator của *Fraud Detection Handbook* (profile khách hàng & terminal → sinh giao dịch theo thời gian → chèn kịch bản fraud). License repo không rõ → tự viết lại theo ý tưởng, ghi nguồn.
- **Bối cảnh VN:** kênh QR/ví/thẻ, MCC theo ISO 18245, ngân hàng phát hành, tỉnh/thành (HCM/HN chiếm phần lớn), giờ cao điểm, ngày lương, Tết, 11/11.
- **Dòng thời gian gốc:** 7/2025–6/2026, có Tết thật và drift từ 1/4; T0 = 1/2/2026. Dòng thời gian này dùng cho train/calibration của model và giữ làm **demo**.
- **Thế giới:** vai trò tài khoản (cá nhân có người nhận quen / hộ kinh doanh nhận tiền qua tài khoản cá nhân / mule nội bộ chuyển tiền ra trong vài giờ), 4 kiểu fraud (card testing, ATO, collusion, social engineering) + cash-out, nhãn trễ. Collusion chỉ xuất hiện trước mốc drift để giai đoạn drift có một nguyên nhân.
- **Chi tiết nghiệp vụ (audit 24/10):** thiết bị hợp lệ thay đổi, chữ ký ATO/card testing theo xác suất, nhãn theo kênh (chargeback chỉ thẻ) + friendly fraud, Tết đổi cơ cấu (lì xì), mule thuê, hộ bán online trả nhà cung cấp, refund/reversal tách dòng, merchant quen, retry. Số tiền lừa đảo sát ngưỡng 10 triệu.
- **Config YAML** cho mọi tham số, mỗi tham số ghi nguồn (PaySim, Handbook, báo cáo thị trường).
- **Kiểm định:** integrity check; leakage check (AUC của từng artefact đơn lẻ phải thấp hơn ngưỡng); so phân phối với dữ liệu tham chiếu (KS, biểu đồ chồng) → đưa vào README.

### Social engineering phải "nhìn thấy" được
Kiểu fraud này dùng thiết bị quen, đúng tỉnh, ban ngày → nếu chỉ có feature thiết bị/vị trí/thời gian thì monitoring và agent không có tín hiệu nào. Vì vậy simulator sinh dữ liệu cho **feature phía người nhận**: lần đầu chuyển cho counterparty, fan-in 7 ngày, tỉ trọng chuyển khoản của khách lớn tuổi, chuyển ra trong 24 h, tài khoản mới/ngủ lâu có tiền vào. Model train chưa thấy pattern → vẫn bỏ sót (đúng câu chuyện), nhưng monitoring và agent có tín hiệu để điều tra.

Sự cố bẫy `legit_business_spike`: hộ kinh doanh có fan-in cao một cách hợp lệ.

> *Số đo của bản 24/10 (đã rollback, chỉ tham khảo):* hộ kinh doanh fan-in ~50 **cao hơn** mule (9) → fan-in một mình vô dụng; tỉ lệ chuyển ra trong 24 h tách rõ (mule 1,0 vs ≤ 0,11). 878K giao dịch, 10/10 integrity check, max AUC artefact 0,91.

## 2. Schema và hợp đồng as-of

| Bảng | Cột chính | Ghi chú |
|---|---|---|
| `customers`, `merchants` | profile, vai trò tài khoản | |
| `transactions` | các mốc nghiệp vụ (`created_at`/`authorized_at`/`settled_at`), `event_at`, `available_at`, `status`, `response_code`, `episode_id` | |
| `label_events` | `txn_id`, `event_id`, `label`, `label_status`, `reported_at`, `available_at`, `source` (chargeback / investigator / customer_report), `supersedes_event_id` | `available_at >= reported_at` |
| `business_calendar` | sự kiện, ngày, `lag_days`, `expected_effect` (kỳ vọng theo metric), `scope` (segment bị ảnh hưởng) | Rule/agent được truy vấn; hợp đồng metric ở dưới |
| Truth sự cố | xem mục 5 | **Không** nằm trong DB của agent |

**Quy tắc as-of:**
- Mọi feature và SQL chỉ đọc bản ghi có `available_at <= as_of_time` và đúng `episode_id`. Truy vấn tại t không đổi khi thêm dữ liệu chỉ khả dụng sau t.
- Aggregate theo cửa sổ (ví dụ cash-out 24 h) chỉ dùng khi đã quan sát đủ cửa sổ, hoặc đánh dấu censored.
- Mọi score, alert, truy vấn và kết luận lưu `as_of_time` và phiên bản snapshot.

**Cô lập truth:** truth/injector ở file hoặc DB riêng; tool của agent không có kết nối tới. Đặt tiền tố `_truth` hay dặn trong prompt **không** đủ làm ranh giới truy cập.

### Kỳ vọng mùa vụ theo metric

`expected_effect` là mapping theo `metric_id`; mỗi entry khai báo `aggregation` và thay đổi tương đối dự kiến trên ngày ảnh hưởng:

| `aggregation` | Đại lượng nền mỗi ngày | Kỳ vọng được khai báo |
|---|---|---|
| `sum` | `baseline_value` (count hoặc tổng tiền) | `relative_change` |
| `ratio` | `baseline_numerator`, `baseline_denominator` | `numerator_relative_change`, `denominator_relative_change` |
| `mean` | Tổng giá trị và số quan sát, lưu như tử/mẫu | Hai thay đổi tương đối như `ratio` |

Kỳ vọng là giả định nghiệp vụ khai báo trước, không lấy từ truth hoặc số đo hậu kiểm của episode. Các đại lượng nền theo ngày/segment lấy từ reference đã khóa, đúng as-of. Ngày ngoài phạm vi ảnh hưởng có thay đổi dự kiến bằng 0. Metric thiếu entry, thiếu thành phần nền, mẫu số không hợp lệ hoặc aggregation chưa hỗ trợ → không đủ bằng chứng để tự đóng. PSI/KS không dùng phép pha loãng tuyến tính; cần metric thành phần được hỗ trợ để kiểm tra giả thuyết mùa vụ. Cách tính kỳ vọng cửa sổ: [triage.md](triage.md).

## 3. Nhãn

### Snapshot nhãn tại thời điểm t

| Trạng thái | Điều kiện | Dùng làm nhãn giám sát? | Phạm vi |
|---|---|---|---|
| `confirmed_fraud` | Kết luận fraud đã khả dụng | 1 | MVP |
| `confirmed_legit` | Kết luận hợp lệ đã khả dụng | 0 | MVP |
| `matured_negative` | Tuổi ≥ H, chưa có kết luận khả dụng | 0 theo chính sách; không gọi là chân lý | MVP |
| `pending` | Tuổi < H, chưa có kết luận | **Không** dùng làm 0 | MVP |
| `disputed` | Kết luận mâu thuẫn chưa phân xử | Loại khỏi supervised fit, báo số lượng | *Mở rộng* |

- **MVP:** mỗi giao dịch tối đa một sự kiện kết luận; H = 90 ngày cho mọi kênh. Cột `supersedes_event_id` để sẵn (NULL).
- *Mở rộng:* sửa nhãn append-only qua `supersedes_event_id` (không ghi đè lịch sử), `disputed`, H theo kênh, sensitivity H = 30/60.
- **Cohort đủ tuổi:** tập fit/calibration có nhãn chỉ gồm giao dịch tuổi ≥ H tại T0, lấy cả positive và negative của cohort. Feedback sớm vẫn hiển thị cho điều tra, nhưng đưa cohort non vào train là ablation riêng.
- Mọi báo cáo ghi `as_of_time`, `label_policy_version`, `model_version`.

### Target: nhãn quan sát và truth
- **Target vận hành:** trạng thái fraud theo policy tại snapshot. Calibration trên target này ghi là `observed-label calibration`.
- **Truth simulator:** có gian lận thực sự hay không; **chỉ evaluator đọc được**. Friendly fraud/chargeback không tự đồng nhất với truth hành vi.
- *Mở rộng:* `oracle-label ablation` — fit/calibration bằng truth, không thuộc luồng vận hành.
- Evaluator báo tỷ lệ fraud không bao giờ được báo cáo và fraud báo sau H. Đủ 100% nhãn *sẽ về* vẫn có thể thiếu fraud không bao giờ được báo.

### Label tail
Sinh sự kiện nhãn tới ít nhất `last_transaction_at + H` để đánh giá nhãn trễ của giai đoạn monitoring; **không** sinh thêm giao dịch tương lai cho feature. Khoảng quan sát giao dịch và quan sát nhãn là hai cấu hình riêng; báo cáo muộn hơn horizon vẫn tính trong audit oracle, số liệu theo snapshot ghi rõ censoring.

> *Số đo bản 24/10 (tham khảo):* tại T0, tỉ lệ nhãn *sẽ về* đã về là tháng 10: 100%, 11: 89%, 12: 45%, 1: 29%. Các số này không tự chứng minh calibration đúng/sai và không đo fraud không bao giờ được báo.

## 4. Episode — "thế giới song song"

- Giữ nguyên thế giới (khách hàng, merchant, hành vi, mạng người nhận) và lịch sử trước T0; **sinh lại 30 ngày sau T0** cho mỗi episode với seed riêng và 0–2 sự cố chèn vào.
- **Pilot 60 episode:** 20 clean + 40 injected, trong đó 4 episode hai nguyên nhân. Số episode cuối cùng do ngân sách LLM quyết định, 60 là sàn ([MASTER_PLAN.md](../MASTER_PLAN.md) mục Ngân sách). Chia vai trò development/calibration/test: [evaluation.md](evaluation.md).
- Ngày tháng trong episode là danh nghĩa: không có lịch cố định (không có Tết); sự kiện mùa vụ được chèn và ghi vào `business_calendar` — trừ biến thể "trend không có trong lịch".

### Kịch bản sự cố

| Kịch bản | Nguyên nhân thật | Hành vi mong đợi |
|---|---|---|
| Giao dịch tăng mạnh dịp lễ/ngày lương/khuyến mãi | `seasonal` | Benign ứng viên; chỉ tự đóng qua gate |
| Hộ kinh doanh nổ đơn hợp lệ (`legit_business_spike`) | `legit_business_change` | Chẩn đoán benign; không có runbook benign áp dụng thì chuyển người theo giới hạn MVP ở [triage.md](triage.md) |
| Card testing / ATO tăng đột biến (kiểu đã có trong train) | `fraud_surge` | Báo fraud ops |
| `amount` nhân 100 hoặc NULL hàng loạt từ một ngày | `data_pipeline` | Báo đội data |
| Merchant mới, ít giao dịch | Truth độc lập với cỡ mẫu | Recheck/chuyển người; có fraud thật thì vẫn chấm theo truth fraud |
| Social engineering | `new_fraud` | Chuyển người + đề xuất retrain |
| Fraud mới + mùa vụ cùng lúc | Hai nguyên nhân | Chuyển người |
| Kẻ gian bắt chước hành vi bình thường | Concept drift "im lặng" | Kiểm tra agent có phát hiện không (ca khó) |

Taxonomy nguyên nhân đầy đủ: [triage.md](triage.md).

## 5. Bảng truth sự cố

Injector ghi ngay khi chèn sự cố; chỉ evaluator đọc.

| Cột | Ý nghĩa |
|---|---|
| `incident_truth_id`, `episode_id` | |
| `cause`, `requires_intervention` | `requires_intervention` = fraud hoặc lỗi pipeline có tác động theo cấu hình |
| `mechanism`, `rule_covered` | Phục vụ tách known/unknown |
| Phạm vi: entity, segment, feature bị tác động (trực tiếp và gián tiếp dự kiến), khoảng thời gian | Dùng cho ghép đáp án |
| `effect_available_at` | `available_at` của giao dịch chèn đầu tiên |
| `intervention_start_at` | = `effect_available_at` |
| `deadline_at` | `intervention_start_at + D`; D = 7 ngày (MVP), sensitivity 3/14 |
| `accepted_actions` | Theo bảng dưới |
| `parent_scenario` | Liên kết hai sự cố thành phần của episode hai nguyên nhân |

| Nguyên nhân | `accepted_actions` | Ghi chú |
|---|---|---|
| `fraud_surge` | `fraud_ops`, `human_review` | |
| `new_fraud` | `human_review`, `fraud_ops` | Đề xuất retrain ghi kèm, không chấm |
| `data_pipeline` | `data_ticket`, `human_review` | |
| Benign (`seasonal`, `legit_business_change`, `no_material_change`) | — | Không cần can thiệp; đóng đúng tính benign automation |
| Hai nguyên nhân | Mỗi sự cố thành phần theo bảng trên | `human_review` thỏa cả hai |

D là giả định nghiệp vụ khai báo trước, không tune. Cách evaluator dùng các cột này: [evaluation.md](evaluation.md).
