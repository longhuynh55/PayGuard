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

P1.4 vòng 1 hiện thực card testing trên `channel = 'card'`: nhiều lần thử số tiền nhỏ trong một cửa sổ ngắn, sau đó là một giao dịch lớn hơn; kết quả authorization được lấy mẫu theo xác suất trong config. Chiến dịch bắt đầu từ ngày mở lịch sử nền và lặp theo `campaign_interval_days` tới trước T0; fraud sau T0 thuộc episode và P1.7. Bảng hiện chưa có card token, nên `customer_id` là proxy quan sát được cho cùng một thẻ trong chuỗi này; đây là giới hạn simulator, không phải khẳng định nghiệp vụ. Ca benign đối chứng có nhịp giao dịch nhỏ tương tự nhưng không có giao dịch lớn tiếp nối. Các giá trị và provenance của generator nằm trong [`configs/simulator.yaml`](../configs/simulator.yaml).

### Social engineering phải "nhìn thấy" được
Kiểu fraud này dùng thiết bị quen, đúng tỉnh, ban ngày → nếu chỉ có feature thiết bị/vị trí/thời gian thì monitoring và agent không có tín hiệu nào. Vì vậy simulator sinh dữ liệu cho **feature phía người nhận**: lần đầu chuyển cho counterparty, fan-in 7 ngày, tỉ trọng chuyển khoản của khách lớn tuổi, chuyển ra trong 24 h, tài khoản mới/ngủ lâu có tiền vào. Model train chưa thấy pattern → vẫn bỏ sót (đúng câu chuyện), nhưng monitoring và agent có tín hiệu để điều tra.

Sự cố bẫy `legit_business_spike`: hộ kinh doanh có fan-in cao một cách hợp lệ.

> *Số đo của bản 24/10 (đã rollback, chỉ tham khảo):* hộ kinh doanh fan-in ~50 **cao hơn** mule (9) → fan-in một mình vô dụng; tỉ lệ chuyển ra trong 24 h tách rõ (mule 1,0 vs ≤ 0,11). 878K giao dịch, 10/10 integrity check, max AUC artefact 0,91.

## 2. Schema và hợp đồng as-of

### Biểu diễn lưu trữ P1

P1 lưu operational và private trong **hai SQLite database độc lập**; đường dẫn export lần lượt nằm dưới `operational/` và `private/`. Agent/tool chỉ được nhận kết nối operational ở các stage sau; việc đặt file ở hai đường dẫn khác nhau tự nó không chứng minh quyền truy cập đã bị cô lập. SQLite được chọn để fixture nhỏ dùng thư viện chuẩn, không cần dịch vụ ngoài.

- Các cột timestamp lưu bằng `TEXT` UTC chuẩn hóa thành `YYYY-MM-DDTHH:MM:SS.ffffffZ` (luôn 6 chữ số phần lẻ, luôn `Z`) để so sánh cutoff theo thứ tự từ điển. API ghi nhận timestamp chỉ nhận datetime có timezone hoặc chuỗi ISO có offset; giá trị được đổi sang UTC trước khi ghi. Không nhận datetime mơ hồ/naive.
- Ngày nghiệp vụ lưu `TEXT` dạng `YYYY-MM-DD` theo `Asia/Ho_Chi_Minh`; ngày này không phải timestamp UTC.
- Số tiền lưu `INTEGER` VND khi có giá trị; `transactions.amount` được phép NULL để biểu diễn lỗi pipeline. Không nhân/chia ngầm theo nghìn hay triệu. ID và mã phân loại lưu `TEXT`, giữ nguyên số 0 ở đầu nếu có. JSON lưu thành `TEXT` UTF-8 hợp lệ; các trường JSON bên dưới nêu loại object/array yêu cầu.
- Bảng dữ liệu có thời gian quan sát đều có `episode_id`, `event_at`, `available_at`. `event_at` là thời điểm nghiệp vụ mà bản ghi mô tả; `available_at` là thời điểm bản ghi trở nên đọc được. Dòng giao dịch biểu diễn các thuộc tính biết lúc tạo; chuyển trạng thái được ghi thành event riêng, không cập nhật ngược dòng giao dịch. Với `label_events`, `event_at` kế thừa `transactions.event_at` của giao dịch được kết luận, còn `reported_at` là lúc nguồn báo kết luận.

### Operational tables

Các cột không ghi nullable là `NOT NULL`; mọi cột `PRIMARY KEY` khai báo `NOT NULL` tường minh (SQLite cho phép NULL trong một số khóa chính TEXT nếu không ghi rõ). `PRIMARY KEY` và `UNIQUE` là ràng buộc duy nhất. Không bảng operational nào có cột sự cố, cơ chế fraud ẩn, đáp án injector, seed hay metadata cấu hình generator.

| Bảng | Cột và kiểu | Khóa / ràng buộc |
|---|---|---|
| `episodes` | `episode_id TEXT`, `role TEXT` | PK `episode_id`; `role` thuộc `base`, `development`, `calibration`, `test`; chỉ `episode_id='base'` được có `role='base'`, và ngược lại. Có đúng một hàng sentinel `base`; mỗi episode khác dùng một role đã khóa trong manifest. UPDATE/DELETE bị từ chối sau khi tạo. Không temporal/as-of table. |
| `customers` | `customer_id TEXT`, `episode_id TEXT`, `event_at TEXT`, `available_at TEXT`, `segment TEXT`, `province TEXT`, `age_band TEXT NULL` | PK `customer_id`; FK `episode_id → episodes.episode_id`. ID khách hàng duy nhất toàn database; thông tin nền dùng chung thuộc `base`, thông tin mới của episode thuộc episode đó. Sau insert không được đổi `customer_id` hoặc `episode_id`. |
| `merchants` | `merchant_id TEXT`, `episode_id TEXT`, `event_at TEXT`, `available_at TEXT`, `mcc TEXT`, `province TEXT` | PK `merchant_id`; FK `episode_id → episodes.episode_id`. ID merchant duy nhất toàn database; giữ MCC dạng text để giữ số 0 đầu. Sau insert không được đổi `merchant_id` hoặc `episode_id`. |
| `transactions` | `txn_id TEXT`, `episode_id TEXT`, `customer_id TEXT`, `merchant_id TEXT`, `created_at TEXT`, `event_at TEXT`, `available_at TEXT`, `amount INTEGER NULL`, `channel TEXT` | PK `txn_id`; UNIQUE `(txn_id, episode_id)`; FK episode/customer/merchant. Customer và merchant được tham chiếu phải thuộc `base` hoặc cùng episode với giao dịch. `event_at = created_at`; cột này chỉ chứa thông tin đã biết lúc tạo. `amount`, nếu có, là số nguyên VND. Sau insert không được đổi `txn_id`, `episode_id`, `customer_id` hoặc `merchant_id`. |
| `transaction_events` | `event_id TEXT`, `txn_id TEXT`, `episode_id TEXT`, `event_type TEXT`, `event_at TEXT`, `available_at TEXT`, `response_code TEXT NULL` | PK `event_id`; FK `txn_id → transactions.txn_id` và `episode_id → episodes.episode_id`. `event_type` thuộc `authorized`, `settled`, `declined`, `refunded`, `reversed`; `available_at >= event_at`. Event có `event_at < T0` phải mang `episode_id='base'`; event từ T0 trở đi mang episode riêng. FK parent cho phép event `base` trỏ giao dịch base; event của `e` trỏ giao dịch base hoặc giao dịch của `e`, không giao dịch episode khác. Bảng append-only: UPDATE/DELETE bị từ chối; hiệu chỉnh bằng event mới. |
| `label_events` | `event_id TEXT`, `txn_id TEXT`, `episode_id TEXT`, `event_at TEXT`, `label TEXT`, `label_status TEXT`, `reported_at TEXT`, `available_at TEXT`, `source TEXT`, `supersedes_event_id TEXT NULL` | PK `event_id`; UNIQUE `txn_id` trong MVP (tối đa một kết luận/giao dịch); FK `(txn_id, episode_id) → transactions(txn_id, episode_id)` và FK tự tham chiếu `supersedes_event_id`. `label` là `fraud` hoặc `legit`; `label_status` là `confirmed`; `source` là `chargeback`, `investigator` hoặc `customer_report`; `available_at >= reported_at`; `event_at` phải khớp giao dịch; `supersedes_event_id` phải NULL trong MVP. Sự kiện nhãn chỉ lưu kết luận đã có, không lưu dòng `pending` hay `matured_negative`. |
| `business_calendar` | `calendar_event_id TEXT`, `episode_id TEXT`, `event_at TEXT`, `available_at TEXT`, `business_date TEXT`, `event_name TEXT`, `lag_days INTEGER`, `expected_effect_json TEXT`, `scope_json TEXT` | PK `calendar_event_id`; FK episode. `business_date` theo ngày VN; `event_at` là đầu ngày nghiệp vụ đó đổi sang UTC. `expected_effect_json` là object theo `metric_id`; `scope_json` là object mô tả phạm vi. |

`event_at`, `available_at`, và các cột timestamp không NULL khác dùng định dạng UTC cố định ở trên. `authorized_at`/`settled_at` không phải cột trên `transactions`; trạng thái sau lúc tạo chỉ có thể đọc từ `transaction_events` đã khả dụng tại snapshot. Lifecycle event trước T0 thuộc lịch sử `base`; event từ T0 trở đi là tương lai riêng của episode, kể cả khi `txn_id` tham chiếu giao dịch nền `base`. Các FK được bật trên từng kết nối SQLite. SQLite operational không chứa FK sang database private.

ID và giá trị text operational không được mã hóa template, nguyên nhân sự cố hoặc role thành phần của kịch bản. `episodes.role` là cột công khai duy nhất cho role của episode theo split; mapping template/cause nằm trong private manifest/truth. ID do generator tạo phải trung tính và ổn định theo episode, để không biến tên episode thành nhãn đáp án.

### Private tables

Các bảng sau chỉ tạo trong database private; không có trong danh sách/DDL operational và không được cấp cho agent/tool. Danh sách này bổ sung kiểu vật lý cho các trường truth ở mục 5.

| Bảng private | Cột và kiểu | Khóa / ràng buộc |
|---|---|---|
| `private_episodes` | `episode_id TEXT` | PK `episode_id NOT NULL`; danh mục episode riêng để kiểm tra FK trong database private. |
| `episode_manifest` | `episode_id TEXT`, `role TEXT`, `seed INTEGER`, `template TEXT`, `content_fingerprint TEXT`, `config_fingerprint TEXT`, `config_json TEXT` | PK/FK `episode_id → private_episodes.episode_id`; role thuộc `development`, `calibration`, `test`; template thuộc `clean`, `data_pipeline`, `seasonal`; hai fingerprint là SHA-256 dạng 64 ký tự hex. `content_fingerprint` băm nội dung operational canonical của base + episode; `config_fingerprint` băm object cấu hình trong `config_json`, object này đủ thông tin tái tạo episode. INSERT một lần; UPDATE/DELETE/REPLACE bị từ chối để khóa role và cấu hình sau khi tạo. Bảng chỉ ở private. |
| `run_metadata` | `key TEXT`, `value_json TEXT` | PK `key NOT NULL`; `value_json` là JSON hợp lệ. Seed/config/version generator nằm ở đây, không operational. |
| `incident_truth` | `incident_truth_id TEXT`, `episode_id TEXT`, `cause TEXT`, `requires_intervention INTEGER`, `mechanism TEXT`, `rule_covered INTEGER`, `affected_entities_json TEXT`, `affected_segments_json TEXT`, `affected_features_json TEXT`, `effect_start_at TEXT`, `effect_end_at TEXT NULL`, `effect_available_at TEXT`, `intervention_start_at TEXT`, `deadline_at TEXT`, `accepted_actions_json TEXT`, `parent_scenario TEXT NULL` | PK `incident_truth_id NOT NULL`; FK episode tới `private_episodes.episode_id`; `requires_intervention` và `rule_covered` là boolean 0/1; các cột `_json` là JSON (ba trường phạm vi là array, `accepted_actions` là array); thời gian theo UTC. `parent_scenario` là ID nhóm cha, NULL nếu không có. |

Các tên cột trong bảng dưới đây là interface schema, không phải quyền đọc. `operational` và `private` được export độc lập; chỉ evaluator được nối tới private theo hợp đồng ở mục 5.

### Hợp đồng as-of

**Quy tắc as-of:**
- Mọi feature và SQL chỉ đọc bản ghi có `available_at <= as_of_time`. Truy vấn tại t không đổi khi thêm dữ liệu chỉ khả dụng sau t.
- Với episode `e`, lựa chọn dữ liệu duy nhất là `(episode_id = 'base' AND event_at < T0) UNION (episode_id = e)`, áp điều kiện `available_at <= as_of_time` cho **cả hai nhánh**. P1.2 hiện thực lựa chọn này qua một hàm as-of duy nhất; caller không tự truy vấn/join các bảng episode.
- `e` phải là episode đã đăng ký và khác `base`. Hàm as-of chỉ nhận tên một temporal operational table trong allowlist; `episodes` là bảng manifest, không phải dữ liệu feature/as-of.
- Aggregate theo cửa sổ (ví dụ cash-out 24 h) chỉ dùng khi đã quan sát đủ cửa sổ, hoặc đánh dấu censored.
- Mọi score, alert, truy vấn và kết luận lưu `as_of_time` và phiên bản snapshot.

### Timestamp, ngày nghiệp vụ, tiền và lịch sử episode chung

- Timestamp (`created_at`, `event_at`, `available_at`, `reported_at` và các timestamp tương tự) được lưu dưới dạng UTC. Ngày/giờ cấu hình cho nghiệp vụ được diễn giải trong `Asia/Ho_Chi_Minh` rồi quy đổi sang UTC khi tạo timestamp.
- T0 là đầu ngày nghiệp vụ `2026-02-01` tại `Asia/Ho_Chi_Minh` (`2026-01-31T17:00:00Z`). `business_calendar` lưu ngày nghiệp vụ theo lịch Việt Nam. Ngày chạy job monitoring hằng ngày, ngày lương và ngày khuyến mãi `11/11` đều được xác định theo `Asia/Ho_Chi_Minh`, không theo ngày UTC.
- `amount` khi có giá trị là số nguyên VND; NULL được giữ nguyên để biểu diễn sự cố pipeline. Không nhân/chia ngầm theo nghìn hay triệu. Ví dụ, ngưỡng 10 triệu đồng biểu diễn là `10000000`.
- Lịch sử chung trước T0 chỉ lưu một lần với `episode_id = 'base'`; không sao chép theo episode. Khi đọc episode `e`, chỉ hàm as-of ở trên được hợp lịch sử base trước T0 với dữ liệu của `e`, đồng thời giới hạn khả dụng bằng `available_at <= as_of_time` trên cả hai nhánh. `e` là ID episode, không dùng giá trị dành riêng `'base'`.

Các quyết định timezone, đơn vị tiền và cách tham chiếu lịch sử chung được người dùng chốt cho P1.1 ngày 26/10/2026. Cấu hình runtime và provenance nằm tại [`configs/simulator.yaml`](../configs/simulator.yaml) và [`configs/episodes.yaml`](../configs/episodes.yaml).

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
- Simulator rút độc lập khả năng một giao dịch có kết luận theo `probability_reported`; nếu có, số ngày từ giao dịch tới lúc báo được lấy theo phân phối đều rời rạc trên khoảng đóng `minimum_delay_days`–`maximum_delay_days` (`delay_distribution: uniform_integer`). Giả định dùng chung cho hai lớp truth; giao dịch không được báo không có dòng operational. Tham số và provenance nằm trong [`configs/simulator.yaml`](../configs/simulator.yaml). Generator MVP đặt `available_at = reported_at`; hợp đồng as-of vẫn cho phép dữ liệu fixture có `available_at` muộn hơn `reported_at`.
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
- Episode smoke đều sinh nền giao dịch và card-testing P1.4 sau T0 cùng nhãn trễ; template `clean` nghĩa là không có sự cố P1.6 được chèn, không hàm ý mọi giao dịch đều hợp lệ. Template, seed, manifest và truth chỉ nằm trong private; operational `episodes` chỉ giữ ID + role.
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

## 6. Đầu ra model (`model_scores`)

`model_scores` là bảng operational, mỗi dòng là score của một giao dịch trong một phiên bản model. `event_at` bằng `decision_at`; trong replay P2.5, score được xem là sẵn có ngay tại thời điểm quyết định (`available_at = decision_at`). Bảng chỉ chứa dự đoán và phiên bản policy/model, không chứa nhãn quan sát, truth, template hay metadata injector.

| Cột | Kiểu | Null? | Khóa / ràng buộc |
|---|---|---|---|
| `txn_id` | `TEXT` | Không | Thành phần PK; FK cùng `episode_id` tới `transactions(txn_id, episode_id)` |
| `episode_id` | `TEXT` | Không | Thành phần PK; FK cùng `txn_id`; khác `base` |
| `decision_at` | `TEXT` UTC | Không | Thời điểm quyết định, bằng `transactions.created_at` |
| `event_at` | `TEXT` UTC | Không | Bằng `decision_at`, dùng bởi hợp đồng as-of |
| `available_at` | `TEXT` UTC | Không | Lúc score đọc được; ≥ `decision_at` |
| `raw_score` | `REAL` | Không | Xác suất fraud thô trong `[0, 1]` |
| `calibrated_prob` | `REAL` | Không | Xác suất fraud sau calibration trong `[0, 1]` |
| `conformal_set` | `TEXT` JSON array | Không | Tập con của `legit`, `fraud`; `[]` hợp lệ |
| `decision_region` | `TEXT` | Không | Một trong `legit`, `fraud`, `review`; tập rỗng/đa nhãn ánh xạ thành `review` |
| `model_version` | `TEXT` | Không | Thành phần PK |
| `label_policy_version` | `TEXT` | Không | Phiên bản chính sách nhãn dùng khi huấn luyện |

Khóa chính là `(txn_id, episode_id, model_version)`, cho phép giữ các phiên bản score khác nhau; P2.5 smoke xuất một dòng cho mỗi giao dịch. Bảng có chỉ mục `(episode_id, event_at, available_at)` để P3 đọc theo episode và snapshot.

## 7. Đầu ra monitoring (`daily_metrics`)

`daily_metrics` là bảng kết quả operational của job P3.1; smoke CLI xuất CSV theo schema này. Bảng không đọc hoặc chứa truth, template, cơ chế injector hay metadata private. Mỗi dòng là một metric của một episode tại một snapshot ngày nghiệp vụ.

| Cột | Kiểu | Ý nghĩa |
|---|---|---|
| `metric_id` | `TEXT` | ID metric ổn định, khai báo trong cấu hình monitoring. |
| `episode_id` | `TEXT` | Episode đang được theo dõi; không phải `base`. |
| `as_of_time` | `TEXT` UTC | Thời điểm chụp dữ liệu, cuối ngày nghiệp vụ Việt Nam. |
| `business_date` | `TEXT` | Ngày nghiệp vụ `Asia/Ho_Chi_Minh`, dạng `YYYY-MM-DD`. |
| `feature` | `TEXT` | Feature P2.2 hoặc `decision_region` đối với tỉ lệ review. |
| `segment` | `TEXT` | `all` ở P3.1; metric review ghi `model_version=<id>` để không gộp các phiên bản model. |
| `value` | `REAL NULL` | PSI cho feature hoặc tỉ lệ review trên số model score trong cửa sổ. NULL khi không có mẫu để tính. |
| `reference_window` | `TEXT` | Khoảng ngày nền cố định cho PSI (`YYYY-MM-DD/YYYY-MM-DD`, hai đầu gồm); `not_applicable` cho tỉ lệ review thô vì P2.5 không lưu score của base. |
| `window_start` | `TEXT` | Ngày đầu cửa sổ nghiệp vụ 7 ngày, gồm cả ngày này. |
| `window_end` | `TEXT` | Ngày cuối cửa sổ nghiệp vụ, gồm cả ngày này. |
| `n_window` | `INTEGER` | Số feature value hữu hạn hoặc số model score trong cửa sổ, tùy metric. |
| `n_reference` | `INTEGER` | Số feature value hữu hạn của base reference; bằng 0 với tỉ lệ review. |
| `flags` | `TEXT` JSON array | Cờ chất lượng như `window_incomplete`, `small_window_sample`, `small_reference_sample`, `no_window_samples`, `no_reference_samples`, `window_censored_samples=<số>`, `reference_censored_samples=<số>`, `target_unavailable_at_decision=<số>`. Không thay các cờ này bằng giá trị mặc định im lặng. |

P3.1 chỉ tính PSI cho feature có cờ quan sát tương ứng của P2.2: `txn_count_24h` dùng `history_window_observed_24h`, `amount_avg_7d` dùng `history_window_observed_7d`. Mẫu có cờ bằng 0 bị loại khỏi phân phối PSI ở cả cửa sổ lẫn reference; `n_window` và `n_reference` đếm mẫu còn lại sau lọc. Nếu có mẫu bị loại, `flags` chứa `window_censored_samples=<số>` hoặc `reference_censored_samples=<số>`. Đây là các cờ do P2.2 tính; P3 không tự suy ra lại tuổi lịch sử. `window_incomplete` tiếp tục đánh dấu cửa sổ 7 ngày chưa đủ ngày quan sát kể từ T0.

Khóa logic là `(metric_id, episode_id, as_of_time, feature, segment)`. Mọi nguồn dữ liệu temporal để tính bảng này, gồm `model_scores`, phải được đọc qua `read_as_of`; dòng chỉ khả dụng sau `as_of_time` không được xuất hiện. Bảng thuộc phía operational và không có dữ liệu truth.

### `alerts`

`alerts` là đầu ra signal operational của P3.2. Schema logic của từng alert:

| Cột | Kiểu | Ý nghĩa |
|---|---|---|
| `alert_id` | `TEXT` | ID trung tính, tất định; SHA-256 rút gọn từ `episode_id`, `as_of_time`, `metric`, `feature`, `segment`, không chứa template hoặc truth. |
| `episode_id` | `TEXT` | Episode của dòng metric đầu vào, khác `base`. |
| `as_of_time` | `TEXT` UTC | Snapshot của `daily_metrics` đã sinh alert. |
| `metric` | `TEXT` | `metric_id` của dòng `daily_metrics`; vòng 1 chỉ chấp nhận metric PSI. |
| `feature` | `TEXT` | Feature P3.1 tương ứng. |
| `segment` | `TEXT` | Segment P3.1 tương ứng. |
| `value` | `REAL` | PSI đo được trên cửa sổ monitoring. |
| `reference` | `TEXT` | `reference_window` của `daily_metrics`; đây là khoảng ngày nền, không phải một giá trị vô hướng riêng. |
| `threshold` | `REAL` | Ngưỡng PSI tại lúc sinh alert; vòng 1 dùng `0.25` với điều kiện nghiêm ngặt `value > threshold`. |
| `event_at` | `TEXT` UTC | Bằng `as_of_time` của dòng metric. |
| `available_at` | `TEXT` UTC | Bằng `as_of_time` của dòng metric; alert được xem là khả dụng tại snapshot đó. |

Khóa chính logic là `alert_id`; mỗi alert ứng với tối đa một dòng `(episode_id, as_of_time, metric, feature, segment)`. Schema này thuộc phía operational và không có truth. Trong vòng 1, `daily_metrics` và `alerts` là đầu vào/đầu ra in-memory hoặc CSV, chưa được lưu thành bảng SQLite đọc qua `read_as_of`; thời điểm lưu vào operational store và thêm allowlist as-of được ghi nhận là việc vòng 2 hoặc trước P4 trong [p3-implementation.md](p3-implementation.md).
