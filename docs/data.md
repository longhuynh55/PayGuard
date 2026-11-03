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

P1.4 vòng 1 hiện thực card testing trên `channel = 'card'`: nhiều lần thử số tiền nhỏ trong một cửa sổ ngắn, sau đó là một giao dịch lớn hơn; kết quả authorization được lấy mẫu theo xác suất trong config. Chiến dịch bắt đầu từ ngày mở lịch sử nền và lặp theo `campaign_interval_days` tới trước T0; fraud sau T0 thuộc episode và P1.7. Vòng 1 dùng `customer_id` làm proxy cho cùng một thẻ; giới hạn này được xử lý ở vòng 2 bằng cột `card_token` trong schema `transactions` bên dưới. Ca benign đối chứng có nhịp giao dịch nhỏ tương tự nhưng không có giao dịch lớn tiếp nối. Các giá trị và provenance của generator nằm trong [`configs/simulator.yaml`](../configs/simulator.yaml).

P1.3 vòng 2 sinh thanh toán merchant nền trên `qr`, `wallet`, và `card`; mỗi thanh toán merchant lấy kênh theo `transactions.channel_weights` trong config. Tỷ lệ là giả định synthetic có provenance tại chỗ, không phải ước lượng thị phần Việt Nam. `transactions.channel` giữ vai trò kênh mặc định cho traffic calendar được chèn đơn kênh; A2A dùng `wallet`.

P1.3 vòng 2 cũng sinh chuyển khoản A2A trên `wallet`: `customer_id` là tài khoản gửi, `recipient_customer_id` là tài khoản nhận, còn `merchant_id` là NULL. Hộ kinh doanh nhận tiền vào tài khoản cá nhân và dùng một nhóm merchant nhà cung cấp lặp lại; cá nhân có các tài khoản nhận quen. Các tỷ lệ vai trò, payee, nhà cung cấp và A2A nằm trong config với provenance là giả định synthetic. Role, quan hệ payee/nhà cung cấp và khả năng mule chuyển ra trong cửa sổ vài giờ chỉ nằm trong private/generator metadata; không có role trong ID hoặc cột operational. P1.3 không sinh chuỗi chuyển tiền gian lận của mule.

P1.4 bổ sung ATO với xác suất chữ ký thiết bị mới; xác suất thay đổi thiết bị hợp lệ được lấy riêng trong giao dịch nền nên một thiết bị mới không tự tạo nhãn fraud. Collusion chỉ xuất hiện trước `drift_start_business_date`. Social engineering chuyển tiền từ nhiều người tới tài khoản mule mới khả dụng rồi chuyển tiếp từng khoản sau khi khoản vào đã settle; tài khoản được ghi nhận trước lần nhận đầu đủ một cửa sổ lịch sử bảy ngày và vẫn thuộc nhóm tài khoản mới 30 ngày. Đối chứng `legit_business_spike` nhận cùng số người gửi qua hộ kinh doanh nhưng không cash-out. Cohort theo `world.legitimate_new_accounts_per_base_history` tạo khách hàng hợp lệ sau ngày bắt đầu lịch sử, kèm giao dịch merchant bình thường sớm trong base history và trước cửa sổ reference monitoring; cohort này giữ tài khoản mới hợp lệ trong lịch sử base mà không thêm giao dịch vào các cửa sổ episode hay làm giảm chất lượng mẫu reference. Base gồm ATO, collusion và hoạt động hộ kinh doanh thường; episode chỉ tự sinh ATO. Social engineering và `legit_business_spike` chỉ được thêm bởi injector/template tương ứng. Fraud mới ngoài card-testing dùng dải amount quanh 10 triệu VND trong config. Mechanism, role, nhóm, quan hệ mule và nhãn chỉ nằm trong private `run_metadata`.

P2 model và P3 monitoring giữ các hàng A2A: chúng được tính như giao dịch đi của `customer_id` gửi. Feature hiện tại không cộng giao dịch đến vào lịch sử của `recipient_customer_id`; feature phía người nhận và cash-out thuộc P1.4. Trong vòng 2, P2 features và P3 metrics bỏ qua event refund/reversal trong `transaction_events` và không net amount theo các event đó.

### Social engineering phải "nhìn thấy" được
Kiểu fraud này dùng thiết bị quen, đúng tỉnh, ban ngày → nếu chỉ có feature thiết bị/vị trí/thời gian thì monitoring và agent không có tín hiệu nào. Vì vậy simulator sinh dữ liệu cho **feature phía người nhận**: lần đầu chuyển cho counterparty, fan-in 7 ngày, tỉ trọng chuyển khoản của khách lớn tuổi, chuyển ra trong 24 h, tài khoản mới/ngủ lâu có tiền vào. Model train chưa thấy pattern → vẫn bỏ sót (đúng câu chuyện), nhưng monitoring và agent có tín hiệu để điều tra.

Sự cố bẫy `legit_business_spike`: hộ kinh doanh có fan-in cao một cách hợp lệ.

`simulator.recipient_features.derive_recipient_signals` suy ra fan-in từ các sender riêng biệt, tỉ trọng sender có age band `55+`, tỉ lệ amount đi ra/vào trong 24 h, tài khoản nhận mới/ngủ lâu và giao dịch đầu tiên theo cặp sender-recipient từ snapshot operational as-of. Các tín hiệu không được ghi sẵn vào bảng hay lấy từ metadata private.

> *Số đo của bản 24/10 (đã rollback, chỉ tham khảo):* hộ kinh doanh fan-in ~50 **cao hơn** mule (9) → fan-in một mình vô dụng; tỉ lệ chuyển ra trong 24 h tách rõ (mule 1,0 vs ≤ 0,11). 878K giao dịch, 10/10 integrity check, max AUC artefact 0,91.

## 2. Schema và hợp đồng as-of

### Biểu diễn lưu trữ P1

P1 lưu operational và private trong **hai SQLite database độc lập**; đường dẫn export lần lượt nằm dưới `operational/` và `private/`. Agent/tool chỉ được nhận kết nối operational ở các stage sau; việc đặt file ở hai đường dẫn khác nhau tự nó không chứng minh quyền truy cập đã bị cô lập. SQLite được chọn để fixture nhỏ dùng thư viện chuẩn, không cần dịch vụ ngoài.

- Các cột timestamp lưu bằng `TEXT` UTC chuẩn hóa thành `YYYY-MM-DDTHH:MM:SS.ffffffZ` (luôn 6 chữ số phần lẻ, luôn `Z`) để so sánh cutoff theo thứ tự từ điển. API ghi nhận timestamp chỉ nhận datetime có timezone hoặc chuỗi ISO có offset; giá trị được đổi sang UTC trước khi ghi. Không nhận datetime mơ hồ/naive.
- **JSON canonical — phạm vi thực tế sau CLEANUP-1.** Các chỗ sau dùng [`simulator/common.py`](../simulator/common.py) (`canonical_json`, `canonical_json_bytes`, `canonical_sha256`): khóa object được sắp xếp, phân tách `,` và `:`, `ensure_ascii=False`, `allow_nan=False` (NaN và Infinity bị từ chối). Gồm: fingerprint canonical trong `simulator/reproducibility.py`; hash cấu hình và ID dẫn xuất trong `simulator/validate.py` và `simulator/__main__.py`; hash identity, lock và entry trong `payguard/identity.py`; `content_sha256` trong `model/artifacts.py`; `data_snapshot` và khóa cache trong `triage/agent.py` và `triage/llm.py` (chuỗi prompt của `triage/agent.py` cũng dùng helper này, không băm trực tiếp); ID quyết định và các cột JSON canonical của `decisions` (`scope_json`, `checklist_results_json`, `tool_trace_ids_json`) trong `triage/gate.py`. Với số hữu hạn, byte của các giá trị này không đổi so với trước CLEANUP-1; 196 file JSON và 5 SQLite đã kiểm không có token NaN/Infinity hay REAL ±Inf.
- **Vẫn gọi `json.dumps` trực tiếp (không đổi trong CLEANUP-1):** `monitoring/signals.py:208` và `monitoring/incidents.py:330` (ID alert/incident; mặc định `ensure_ascii=True`, cho phép NaN); `simulator/episodes.py:725` (`config_json`; `config_fingerprint` băm chính chuỗi này); `triage/tools.py:608` (`_snapshot_hash`) và `:624` (`_trace_id`); `model/train.py:99` (băm `fit_rows`; đã có `allow_nan=False`); `monitoring/incidents.py:339` (cột `alert_ids_json`); `triage/tools.py:350` (kích thước kết quả, không băm). Đây không phải kiểm kê đầy đủ mọi `json.dumps` trong repo.
- Ngày nghiệp vụ lưu `TEXT` dạng `YYYY-MM-DD` theo `Asia/Ho_Chi_Minh`; ngày này không phải timestamp UTC.
- Số tiền lưu `INTEGER` VND khi có giá trị; `transactions.amount` được phép NULL để biểu diễn lỗi pipeline. Không nhân/chia ngầm theo nghìn hay triệu. ID và mã phân loại lưu `TEXT`, giữ nguyên số 0 ở đầu nếu có. JSON lưu thành `TEXT` UTF-8 hợp lệ; các trường JSON bên dưới nêu loại object/array yêu cầu.
- Bảng dữ liệu có thời gian quan sát đều có `episode_id`, `event_at`, `available_at`. `event_at` là thời điểm nghiệp vụ mà bản ghi mô tả; `available_at` là thời điểm bản ghi trở nên đọc được. Dòng giao dịch biểu diễn các thuộc tính biết lúc tạo; chuyển trạng thái được ghi thành event riêng, không cập nhật ngược dòng giao dịch. Với `label_events`, `event_at` kế thừa `transactions.event_at` của giao dịch được kết luận, còn `reported_at` là lúc nguồn báo kết luận.

### Operational tables

Các cột không ghi nullable là `NOT NULL`; mọi cột `PRIMARY KEY` khai báo `NOT NULL` tường minh (SQLite cho phép NULL trong một số khóa chính TEXT nếu không ghi rõ). `PRIMARY KEY` và `UNIQUE` là ràng buộc duy nhất. Không bảng operational nào có dữ liệu truth sự cố, cơ chế fraud ẩn, đáp án injector, seed hay metadata cấu hình generator.

| Bảng | Cột và kiểu | Khóa / ràng buộc |
|---|---|---|
| `episodes` | `episode_id TEXT`, `role TEXT` | PK `episode_id`; `role` thuộc `base`, `development`, `calibration`, `test`; chỉ `episode_id='base'` được có `role='base'`, và ngược lại. Có đúng một hàng sentinel `base`; mỗi episode khác dùng một role đã khóa trong manifest. UPDATE/DELETE bị từ chối sau khi tạo. Không temporal/as-of table. |
| `customers` | `customer_id TEXT`, `episode_id TEXT`, `event_at TEXT`, `available_at TEXT`, `segment TEXT`, `province TEXT`, `age_band TEXT NULL` | PK `customer_id`; FK `episode_id → episodes.episode_id`. ID khách hàng duy nhất toàn database, mọi ID do generator tạo có dạng `customer-NNNNNN`; thông tin nền dùng chung thuộc `base`, thông tin mới của episode thuộc episode đó. Sau insert không được đổi `customer_id` hoặc `episode_id`. |
| `merchants` | `merchant_id TEXT`, `episode_id TEXT`, `event_at TEXT`, `available_at TEXT`, `mcc TEXT`, `province TEXT` | PK `merchant_id`; FK `episode_id → episodes.episode_id`. ID merchant duy nhất toàn database; giữ MCC dạng text để giữ số 0 đầu. Sau insert không được đổi `merchant_id` hoặc `episode_id`. |
| `transactions` | `txn_id TEXT`, `episode_id TEXT`, `customer_id TEXT`, `merchant_id TEXT NULL`, `recipient_customer_id TEXT NULL`, `device_id TEXT NULL`, `card_token TEXT NULL`, `created_at TEXT`, `event_at TEXT`, `available_at TEXT`, `amount INTEGER NULL`, `channel TEXT` | PK `txn_id`; UNIQUE `(txn_id, episode_id)`; FK episode/customer/merchant/recipient customer. Mọi `txn_id` do generator tạo có dạng `txn-<64 lowercase hex>` cho cả nền, injector, fraud và control. `customer_id` là tài khoản gửi. Thanh toán merchant có `merchant_id` khác NULL và `recipient_customer_id` NULL; A2A có `merchant_id` NULL và `recipient_customer_id` khác NULL, không tự chuyển cho chính mình. Các entity được tham chiếu phải thuộc `base` hoặc cùng episode với giao dịch. `device_id` và `card_token` là token quan sát được, biết lúc tạo; không chứa role, mechanism hay nhãn. Cả hai có thể NULL; nếu `card_token` có giá trị thì `channel='card'`, còn kênh khác bắt buộc NULL. `event_at = created_at`; dòng chỉ chứa thông tin biết lúc tạo. `amount`, nếu có, là số nguyên VND. `channel` thuộc `qr`, `wallet`, `card`; A2A P1.3 dùng `wallet`. Sau insert không được đổi cột nhận dạng/liên kết, `device_id` hoặc `card_token`. |
| `transaction_events` | `event_id TEXT`, `txn_id TEXT`, `episode_id TEXT`, `event_type TEXT`, `event_at TEXT`, `available_at TEXT`, `amount INTEGER NULL`, `response_code TEXT NULL` | PK `event_id`; FK `txn_id → transactions.txn_id` và `episode_id → episodes.episode_id`; `txn_id` liên kết event refund/reversal với giao dịch gốc. `event_type` thuộc `authorized`, `settled`, `declined`, `refunded`, `reversed`. `amount` NULL cho các event không phải refund/reversal; mỗi giao dịch tối đa một adjustment, amount dương và tổng adjustment không vượt amount gốc tại lúc tạo giao dịch. Adjustment phải có event `settled` xảy ra trước nó và khả dụng không muộn hơn adjustment. `event_at` của adjustment phải sau `transactions.created_at` của giao dịch gốc. Trigger kiểm tra amount gốc lúc chèn event; validator dùng private pre-injection amount cho các giao dịch pipeline bị đổi amount/NULL. `available_at >= event_at`; generator smoke đặt `available_at = event_at`. Event có `event_at < T0` phải mang `episode_id='base'`; event từ T0 trở đi mang episode riêng. Với `episode_id != 'base'`, cả `event_at` và `available_at` phải nằm trong episode future window `[T0, T0 + duration_days)`, kể cả authorization, settlement và refund/reversal. FK parent cho phép event `base` trỏ giao dịch base; event của `e` trỏ giao dịch base hoặc giao dịch của `e`, không giao dịch episode khác. Bảng append-only: UPDATE/DELETE bị từ chối; hiệu chỉnh bằng event mới. |
| `label_events` | `event_id TEXT`, `txn_id TEXT`, `episode_id TEXT`, `event_at TEXT`, `label TEXT`, `label_status TEXT`, `reported_at TEXT`, `available_at TEXT`, `source TEXT`, `supersedes_event_id TEXT NULL` | PK `event_id`; UNIQUE `txn_id` trong MVP (tối đa một kết luận/giao dịch); FK `(txn_id, episode_id) → transactions(txn_id, episode_id)` và FK tự tham chiếu `supersedes_event_id`. `label` là `fraud` hoặc `legit`; `label_status` là `confirmed`; `source` là `chargeback`, `investigator` hoặc `customer_report`; `available_at >= reported_at`; `event_at` phải khớp giao dịch; `supersedes_event_id` phải NULL trong MVP. Sự kiện nhãn chỉ lưu kết luận đã có, không lưu dòng `pending` hay `matured_negative`. |
| `business_calendar` | `calendar_event_id TEXT`, `episode_id TEXT`, `event_at TEXT`, `available_at TEXT`, `business_date TEXT`, `event_name TEXT`, `lag_days INTEGER`, `expected_effect_json TEXT`, `scope_json TEXT` | PK `calendar_event_id`; FK episode. `business_date` theo ngày VN; `event_at` là đầu ngày nghiệp vụ đó đổi sang UTC. `expected_effect_json` là object theo `metric_id`; `scope_json` là object mô tả phạm vi. Với `txn_count`, `relative_change` bằng số giao dịch bơm trong scope chia cho nền scope, theo [mục 4](#biên-độ-bơm-theo-tỉ-lệ-nền-sim-scale). |
| `daily_metrics` | `metric_id TEXT`, `episode_id TEXT`, `as_of_time TEXT`, `event_at TEXT`, `available_at TEXT`, `business_date TEXT`, `feature TEXT`, `segment TEXT`, `value REAL NULL`, `reference_window TEXT`, `window_start TEXT`, `window_end TEXT`, `n_window INTEGER`, `n_reference INTEGER`, `flags TEXT` JSON array | PK `(metric_id, episode_id, as_of_time, feature, segment)`; FK episode; `event_at = available_at = as_of_time`; snapshot append-only. |
| `alerts` | `alert_id TEXT`, `episode_id TEXT`, `as_of_time TEXT`, `metric TEXT`, `feature TEXT`, `segment TEXT`, `value REAL`, `reference TEXT`, `threshold REAL`, `event_at TEXT`, `available_at TEXT` | PK `alert_id`; UNIQUE `(episode_id, as_of_time, metric, feature, segment)`; FK episode và `(metric, episode_id, as_of_time, feature, segment) → daily_metrics(metric_id, episode_id, as_of_time, feature, segment)`; `event_at = available_at = as_of_time`; snapshot append-only. |
| `incidents` | `incident_id TEXT`, `parent_incident_id TEXT NULL`, `episode_id TEXT`, `alert_ids_json TEXT` JSON array, `feature TEXT`, `segment TEXT`, `opened_at TEXT`, `status TEXT`, `event_at TEXT`, `available_at TEXT` | PK `(incident_id, event_at)`; FK episode; mỗi alert trong `alert_ids_json` phải thuộc cùng episode; `parent_incident_id`, nếu có, chỉ đến incident cùng episode; snapshot append-only. |

`event_at`, `available_at`, và các cột timestamp không NULL khác dùng định dạng UTC cố định ở trên. `authorized_at`/`settled_at` không phải cột trên `transactions`; trạng thái sau lúc tạo chỉ có thể đọc từ `transaction_events` đã khả dụng tại snapshot. Lifecycle event trước T0 thuộc lịch sử `base`; event từ T0 trở đi là tương lai riêng của episode, kể cả khi `txn_id` tham chiếu giao dịch nền `base`. Các FK được bật trên từng kết nối SQLite. SQLite operational không chứa FK sang database private.

ID và giá trị text operational không được mã hóa template, nguyên nhân sự cố hoặc role thành phần của kịch bản. `episodes.role` là cột công khai duy nhất cho role của episode theo split; mapping template/cause nằm trong private manifest/truth. ID do generator tạo phải trung tính và ổn định theo episode, để không biến tên episode thành nhãn đáp án.

### Private tables

Các bảng sau chỉ tạo trong database private; không có trong danh sách/DDL operational và không được cấp cho agent/tool. Danh sách này bổ sung kiểu vật lý cho các trường truth ở mục 5.

| Bảng private | Cột và kiểu | Khóa / ràng buộc |
|---|---|---|
| `private_episodes` | `episode_id TEXT` | PK `episode_id NOT NULL`; danh mục episode riêng để kiểm tra FK trong database private. |
| `episode_manifest` | `episode_id TEXT`, `role TEXT`, `seed INTEGER`, `template TEXT`, `content_fingerprint TEXT`, `config_fingerprint TEXT`, `config_json TEXT` | PK/FK `episode_id → private_episodes.episode_id`; role thuộc `development`, `calibration`, `test`; template thuộc `clean`, `data_pipeline`, `seasonal`, `fraud_surge`, `fraud_calendar`, `two_cause_fraud_seasonal`, `new_fraud`, `legit_business_spike`, `new_merchant_sparse_history`, `silent_concept_drift`; hai fingerprint là SHA-256 dạng 64 ký tự hex. `content_fingerprint` băm nội dung operational canonical của base + episode; `config_fingerprint` băm object cấu hình trong `config_json`, object này đủ thông tin tái tạo episode. INSERT một lần; UPDATE/DELETE/REPLACE bị từ chối để khóa role và cấu hình sau khi tạo. Bảng chỉ ở private. |
| `run_metadata` | `key TEXT`, `value_json TEXT` | PK `key NOT NULL`; `value_json` là JSON hợp lệ. Seed/config/version generator nằm ở đây, không operational. |
| `incident_truth` | `incident_truth_id TEXT`, `episode_id TEXT`, `cause TEXT`, `requires_intervention INTEGER`, `mechanism TEXT`, `rule_covered INTEGER`, `affected_entities_json TEXT`, `affected_segments_json TEXT`, `affected_features_json TEXT`, `effect_start_at TEXT`, `effect_end_at TEXT NULL`, `effect_available_at TEXT`, `intervention_start_at TEXT`, `deadline_at TEXT`, `accepted_actions_json TEXT`, `parent_scenario TEXT NULL` | PK `incident_truth_id NOT NULL`; FK episode tới `private_episodes.episode_id`; `requires_intervention` và `rule_covered` là boolean 0/1; các cột `_json` là JSON (ba trường phạm vi là array, `accepted_actions` là array); thời gian theo UTC. `parent_scenario` là ID nhóm cha, NULL nếu không có. |

**Ghi chú SIM-SCALE (review r1 F1–F2, 03/11/2026):** với template tác động lượng (`seasonal`, `fraud_surge`, `fraud_calendar`, `two_cause_fraud_seasonal`), số giao dịch bơm và kỳ vọng lịch còn phụ thuộc nền reference cấp run (`run_metadata` `resolved_config.volume_reference`: cửa sổ + trung vị tổng/kênh), nên `config_json` của episode cộng metadata cấp run này mới đủ tái tạo episode. Cờ `simulator generate --monitoring-config` chỉ dùng cho test: run sinh với cửa sổ reference khác `configs/monitoring.yaml` vẫn tự nhất quán khi validate nhưng lệch với nền monitoring mặc định (backlog: validator đối chiếu với config monitoring mặc định).

Enum template trong `episode_manifest` và CHECK private khớp danh sách template ở trên. `fraud_calendar` chỉ có truth `fraud_surge` và một calendar row lấy tên và kênh từ cấu hình seasonal; `relative_change` của row là tác động bơm thực trong scope kênh, bằng 0 cho `fraud_calendar` vì burst là giao dịch card (row trung tính, khớp dòng `fraud_calendar` ở bảng kịch bản; xem [mục 4](#biên-độ-bơm-theo-tỉ-lệ-nền-sim-scale)); nó không chèn seasonal traffic/truth. `two_cause_fraud_seasonal` là một episode có đúng hai truth thành phần cùng `parent_scenario`. Các template pilot còn lại ánh xạ một-một sang truth tương ứng trong mục này; merchant thưa dùng `no_material_change` theo quyết định MASTER mục 7.

P1.3 lưu account profile tại `run_metadata` key `p1.3.account_profiles.v1`: object gồm `account_roles` (customer ID → role), `familiar_payees` (customer ID → customer ID array), `supplier_merchants` (customer ID → merchant ID array), và `internal_mule_outbound_window_hours`. Key này chỉ ở private; transaction row không mang account role. P1.4 lưu cohort ID tại `p1.4.legitimate_new_accounts.v1` cùng mechanism, group, participant và quan hệ transaction trong private `run_metadata`. P1.7 lưu N7 cohort tại `p1.7.legitimate_new_accounts.v1:<episode_id>`: deterministic seed, customer IDs/role/purpose, creation timestamp, và ordinary transaction IDs; cohort này không phải incident truth. Khóa RNG gốc cho quyết định báo nhãn và độ trễ được lưu theo từng transaction trong private `p1.5.label_truth.v1`; validator có thể phát lại quyết định này ở tiến trình riêng. Với `data_pipeline`, metadata private `p1.6.incident_effect.v1:<incident_truth_id>` cũng lưu `pre_injection_amount_by_txn_id` cho đúng các transaction trong `affected_transaction_ids`; validator dùng amount gốc này để kiểm tra hiệu ứng injector và adjustment event sau khi operational amount bị nhân hoặc đặt NULL. Giá trị này không được chép vào operational.

Recipient signals and their as-of contract are described in [§1, Simulator nền](#1-simulator-n%E1%BB%81n). This section defines schema and private metadata only.

Các tên cột trong bảng dưới đây là interface schema, không phải quyền đọc. `operational` và `private` được export độc lập; chỉ evaluator được nối tới private theo hợp đồng ở mục 5.

### Hợp đồng as-of

**Quy tắc as-of:**
- Mọi feature và SQL chỉ đọc bản ghi có `available_at <= as_of_time`. Truy vấn tại t không đổi khi thêm dữ liệu chỉ khả dụng sau t.
- Với episode `e`, lựa chọn dữ liệu duy nhất là `(episode_id = 'base' AND event_at < T0) UNION (episode_id = e)`, áp điều kiện `available_at <= as_of_time` cho **cả hai nhánh**. P1.2 hiện thực lựa chọn này qua một hàm as-of duy nhất; caller không tự truy vấn/join các bảng episode.
- `e` phải là episode đã đăng ký và khác `base`. Hàm as-of chỉ nhận tên một temporal operational table trong allowlist; `episodes` là bảng manifest, không phải dữ liệu feature/as-of.
- Aggregate theo cửa sổ (ví dụ cash-out 24 h) chỉ dùng khi đã quan sát đủ cửa sổ, hoặc đánh dấu censored.
- Mọi score, alert, truy vấn và kết luận lưu `as_of_time` và phiên bản snapshot.

**Chỉ mục đọc as-of:** `transactions`, `customers`, `label_events`, `model_scores`, `daily_metrics`, `alerts`, `incidents` và `decisions` có chỉ mục `<table>_episode_asof` trên `(episode_id, event_at, available_at)`. Chỉ mục chỉ đổi kế hoạch truy vấn: `read_as_of` giữ nguyên điều kiện lọc, `ORDER BY event_at, rowid`, cột và kết quả; không đổi khóa, dữ liệu hay fingerprint. Chỉ mục là một phần hợp đồng schema mà `simulator validate` kiểm (`_check_schema`): export sinh trước PERF-STORAGE (03/11/2026) không còn qua validate, phải sinh lại. `transaction_events`, `merchants` và `business_calendar` chưa có chỉ mục as-of (xem bàn giao PERF-STORAGE trong [p5-implementation.md](p5-implementation.md)).

### Timestamp, ngày nghiệp vụ, tiền và lịch sử episode chung

- Timestamp (`created_at`, `event_at`, `available_at`, `reported_at` và các timestamp tương tự) được lưu dưới dạng UTC. Ngày/giờ cấu hình cho nghiệp vụ được diễn giải trong `Asia/Ho_Chi_Minh` rồi quy đổi sang UTC khi tạo timestamp.
- T0 là đầu ngày nghiệp vụ `2026-02-01` tại `Asia/Ho_Chi_Minh` (`2026-01-31T17:00:00Z`). T0 là hằng số hợp đồng: validate cấu hình từ chối `timeline.t0_business_date` khác `2026-02-01`, vì trigger `transaction_events_episode_boundary` và snapshot triage dùng cùng T0 cố định. `business_calendar` lưu ngày nghiệp vụ theo lịch Việt Nam. Ngày chạy job monitoring hằng ngày, ngày lương và ngày khuyến mãi `11/11` đều được xác định theo `Asia/Ho_Chi_Minh`, không theo ngày UTC.
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

Kỳ vọng `txn_count` của lịch mùa vụ là tác động đã bơm trên scope, tính theo [mục 4](#biên-độ-bơm-theo-tỉ-lệ-nền-sim-scale) (SIM-SCALE 03/11/2026), không phải hằng số khai báo sẵn; nó không đọc truth hay số đo hậu kiểm của monitoring, alert hay score. Các đại lượng nền theo ngày/segment lấy từ reference đã khóa, đúng as-of. Ngày ngoài phạm vi ảnh hưởng có thay đổi dự kiến bằng 0. Metric thiếu entry, thiếu thành phần nền, mẫu số không hợp lệ hoặc aggregation chưa hỗ trợ → không đủ bằng chứng để tự đóng. PSI/KS không dùng phép pha loãng tuyến tính; cần metric thành phần được hỗ trợ để kiểm tra giả thuyết mùa vụ. Cách tính kỳ vọng cửa sổ: [triage.md](triage.md).

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
- **Pilot 60 episode:** 20 clean + 40 injected (role 12/24/24, clean 4/8/8). **Cơ cấu injected — người dùng chốt 31/10/2026** (thay "4 episode hai nguyên nhân" bản đầu; lý do: [P5.0a](p5-implementation.md) chỉ ra so sánh UQ chỉ khác 0 ở ca fraud trùng ngày lịch): **10** episode hai nguyên nhân fraud + mùa vụ (dev 2 / cal 4 / test 4) và **5** episode fraud một nguyên nhân trùng ngày `business_calendar` (1 / 2 / 2); bớt tương ứng ở nhóm rule đã phủ (`data_pipeline`, `fraud_surge`), mỗi nhóm vẫn ≥ 1 dev / 2 cal / 2 test. Tỉ lệ gộp vì vậy không đại diện phân phối tự nhiên: luôn báo theo nhóm và ghi cơ cấu.
  - **Bảng phân bổ 60 episode (người dùng duyệt 31/10/2026, audit B3):**

    | Kịch bản | dev | cal | test | Tổng |
    |---|---|---|---|---|
    | clean | 4 | 8 | 8 | 20 |
    | hai nguyên nhân fraud + mùa vụ | 2 | 4 | 4 | 10 |
    | fraud một nguyên nhân trùng ngày lịch | 1 | 2 | 2 | 5 |
    | seasonal đơn | 1 | 2 | 2 | 5 |
    | `data_pipeline` | 1 | 2 | 2 | 5 |
    | `fraud_surge` | 1 | 2 | 2 | 5 |
    | `new_fraud` (social engineering) | 1 | 1 | 1 | 3 |
    | `legit_business_spike` | 1 | 1 | 1 | 3 |
    | merchant mới, ít giao dịch | 0 | 1 | 1 | 2 |
    | concept drift "im lặng" | 0 | 1 | 1 | 2 |
    | **Tổng** | **12** | **24** | **24** | **60** |

    Merchant mới và concept drift không có episode development: **chỉ đánh giá, không viết rule/ngưỡng trên chúng** (ngưỡng `n_min` đã chọn trên clean development; drift là ca khó của agent). Bảng này thay "mỗi nhóm ≥ 1/2/2" ở câu trên cho `new_fraud`/`legit_business_spike`. Số episode cuối cùng do ngân sách LLM quyết định, 60 là sàn ([MASTER_PLAN.md](../MASTER_PLAN.md) mục Ngân sách). Chia vai trò development/calibration/test: [evaluation.md](evaluation.md).
- Seed smoke giữ nguyên phép dẫn xuất `p1.7.episode.v1(root_seed, episode_id)` để giữ nguyên baseline. Pilot dùng `p1.7.episode.v2(root_seed, profile, episode_id)`; validator xác nhận seed calibration/test không giao với manifest smoke.
- **Đã bị thay thế bởi quyết định 01/11/2026:** theo quyết định N7 trước đó trong MASTER mục 7, mỗi episode không clean có hai tài khoản cá nhân hợp lệ được tạo trong tương lai của episode. `configs/episodes.yaml` lưu số lượng, thời điểm tạo/giao dịch, lookback tối thiểu và provenance; giao dịch của cohort bắt đầu sau khi account đã đủ bảy ngày tuổi, nên observation flags của feature 7 ngày không bị censor. Membership và transaction IDs chỉ ghi trong private `run_metadata`.
- **Quyết định N7 ngày 01/11/2026 (người dùng chốt; review P1.7 r1 N5):** áp dụng hai tài khoản hợp lệ cho mọi episode, kể cả clean. N7 được chèn sau template injector để giữ nguyên đầu ra P1.6. Bản triển khai đầu tiên gán tài khoản đầu cùng timestamp với mule và cho giao dịch đầu ngày 27; các chi tiết đó đã bị thay thế bởi P1.7 Fix 2 sau review r2 F1′.
- **Hành vi N7 hiện tại (P1.7 Fix 2):** `creation_offset_days` áp dụng nguyên vẹn trong mọi template, không sao chép timestamp mule. Tài khoản hợp lệ sớm bắt đầu giao dịch merchant thường ngày 15 sau T0, sau lookback bảy ngày và trước deadline ngày 15; một tài khoản theo lịch social-engineering bắt đầu ngày 8; tài khoản hợp lệ trong-window bắt đầu ngày 27. Episode không có social-engineering mule nhận một tài khoản ordinary theo lịch social-engineering đã seed; episode `new_fraud` có mule đó. Như vậy mọi episode có cùng ba customer sau T0, không có timestamp tạo trùng, và cùng phân phối ngày giao dịch đầu tiên `[8, 15, 27]`. Control transaction amounts được lấy từ miền amount cấu hình như các giao dịch nền. Customer/timestamp controls được tạo sau template injector; giao dịch legitimate đến sau thời điểm injector đã chụp phạm vi tác động và không được sửa bởi `data_pipeline`. Validator kiểm cả customer và transaction đúng episode theo `available_at <= deadline_at`; tập transaction chạm customer sau T0 phải là strict superset của `affected_transaction_ids` trong truth `new_fraud`. Cấu hình đầy đủ và provenance ở [`configs/episodes.yaml`](../configs/episodes.yaml); cohort và membership chỉ nằm trong private `run_metadata`.
- Episode smoke đều sinh nền giao dịch và card-testing P1.4 sau T0 cùng nhãn trễ; template `clean` nghĩa là không có sự cố P1.6 được chèn, không hàm ý mọi giao dịch đều hợp lệ. Template, seed, manifest và truth chỉ nằm trong private; operational `episodes` chỉ giữ ID + role.
- Ngày tháng trong episode là danh nghĩa: không có lịch cố định (không có Tết); sự kiện mùa vụ được chèn và ghi vào `business_calendar` — trừ biến thể "trend không có trong lịch".

### Kịch bản sự cố

| Kịch bản | Nguyên nhân thật | Hành vi mong đợi |
|---|---|---|
| Giao dịch tăng mạnh dịp lễ/ngày lương/khuyến mãi | `seasonal` | Benign ứng viên; chỉ tự đóng qua gate |
| Hộ kinh doanh nổ đơn hợp lệ (`legit_business_spike`) | `legit_business_change` | Chẩn đoán benign; không có runbook benign áp dụng thì chuyển người theo giới hạn MVP ở [triage.md](triage.md) |
| Card testing / ATO tăng đột biến (kiểu đã có trong train) | `fraud_surge` | Báo fraud ops; biến thể `fraud_calendar` trùng ngày một calendar row trung tính nhưng không có seasonal truth/lift |
| `amount` nhân 100 hoặc NULL hàng loạt từ một ngày | `data_pipeline` | Báo đội data |
| Merchant mới, ít giao dịch | `no_material_change` trong pilot; injector vẫn giữ nhánh `new_fraud` ngoài pilot | Recheck/chuyển người; pilot benign có `requires_intervention = 0`, `accepted_actions = []` |
| Social engineering | `new_fraud`, mechanism `social_engineering` | Chuyển người + đề xuất retrain |
| Fraud mới + mùa vụ cùng lúc | Hai truth `fraud_surge` + `seasonal` cùng `parent_scenario` | Chuyển người |
| Kẻ gian bắt chước hành vi bình thường | Cause `new_fraud`, mechanism `silent_concept_drift` | Kiểm tra agent có phát hiện không (ca khó) |

Taxonomy nguyên nhân đầy đủ: [triage.md](triage.md).

Social-engineering chains chỉ được injector của template `new_fraud` sinh qua writer P1.4. Spike hộ kinh doanh chỉ được injector template `legit_business_spike` sinh; lịch sử `base` giữ hoạt động fan-in hộ kinh doanh thông thường làm đối chứng, không có chuỗi social engineering.

### Biên độ bơm theo tỉ lệ nền (SIM-SCALE)

Các template tác động lượng (`seasonal`, `fraud_surge`, `fraud_calendar`, phần mùa vụ và phần fraud của `two_cause_fraud_seasonal`) bơm:

`số bơm = max(sàn smoke, round(relative_amplitude × nền scope))`

- **Sàn smoke** giữ nguyên: `seasonal.additional_transaction_count` (20) và burst card testing `attempts_per_card + 1` (9). Smoke không đổi giao dịch so với trước SIM-SCALE; ở quy mô smoke sàn quyết định.
- **Nền scope** là median số giao dịch theo ngày của base (`episode_id = 'base'`, `available_at <= T0`) trong cửa sổ reference của [configs/monitoring.yaml](../configs/monitoring.yaml), tính cả ngày zero, cùng định nghĩa với `reference_median_1d_count` của monitoring. Nền kênh là cùng phép tính trên một kênh. Lịch mùa vụ dùng nền của kênh `transactions.channel`; burst card testing dùng nền tổng. Nền chỉ đọc base trước T0, không đọc truth, metric hay hàng sau T0; cửa sổ đã dùng được ghi trong `resolved_config.volume_reference` của metadata run.
- **Biên độ khởi đầu** (giả định, provenance `assumption`, người dùng duyệt 03/11/2026, khai báo trong [configs/episodes.yaml](../configs/episodes.yaml)): `seasonal` 1,0 trên kênh scope; `fraud_surge` 0,5 trên tổng; `fraud_calendar` dùng biên độ của `fraud_surge`; `two_cause_fraud_seasonal` dùng hai biên độ trên.
- **Burst card testing:** `N` là tổng số hàng kể cả follow-up, số lần thử nhỏ là `N − 1`. Cửa sổ 10 phút, giờ bắt đầu và jitter giữ nguyên; khoảng cách giữa các lần thử theo luật cửa sổ chia số lần thử với jitter cấu hình, nên burst pilot dày hơn smoke (khoảng 6 giây thay vì 75 giây).
- **Kỳ vọng lịch:** `business_calendar.expected_effect_json.txn_count.relative_change` = số giao dịch bơm trong scope / nền scope, tính từ hàng thực đã chèn; không còn hằng 5,0. Burst card không nằm trong scope `qr`, nên lịch của `fraud_calendar` là trung tính (`relative_change = 0`), khớp dòng "calendar row trung tính" ở bảng kịch bản trên.
- **Không đổi:** `clean`, `data_pipeline`, `new_fraud`, `silent_concept_drift`, `new_merchant_sparse_history`, control N7. `legit_business_spike` **không thuộc SIM-SCALE**: giữ nguyên mẫu fan-in đã khóa, vì đây là bẫy fan-in phía người nhận chứ không phải sự kiện khối lượng (quyết định orchestrator 03/11); phát hiện của nó thuộc feature phía người nhận, hoãn sang monitoring vòng 2 (xem [p1-implementation.md](p1-implementation.md)).

### Báo cáo support P1 (`smoke`, `pilot`)

`generate --profile <p>` ghi hai báo cáo cùng contract, tên theo profile (`<p>` là `smoke` hoặc `pilot`, `report_version = 1`):

| File | Vùng | Nội dung |
|---|---|---|
| `reports/<p>.json` | operational | `profile`, `as_of_time`, `label_policy_version`, `model_version`, `p2_cohort_support` (fit, validation, calibration, reference_drift, monitoring_test), `operational_distributions`, `window_completeness`, `label_delay` |
| `private/reports/<p>_truth.json` | private, chỉ evaluator/validator | `truth_bearing = true`, `truth_label_support`, `label_delay_oracle`, `incident_truth_counts_by_cause`, `p1_4_feature_diagnostics`; riêng `pilot` có thêm `role_template_support` |

- **Ranh giới:** báo cáo operational không chứa khóa thuộc danh sách cấm của validator (truth, template, cause, …). Validator kiểm cả hai file có mặt, đúng `profile` và `report_version` ở cả báo cáo operational lẫn private; báo cáo của profile khác không được có trong cùng run.
- **Held-out (`pilot`):** `p1_4_feature_diagnostics` chỉ có `base` và episode `development`. Episode `calibration` và `test` chỉ có số đếm và thống kê quá trình nhãn/support: giao dịch, nhãn theo trạng thái, truth theo cause, `label_delay`, `label_delay_oracle`, `operational_distributions` theo kênh/thời gian, `window_completeness`, `truth_label_support` (orchestrator chốt 02/11 sau review P1.8 r1: tham số trễ nhãn và H đã khóa, giống mọi episode). Không có AUC, feature diagnostic, metric, alert hay score cho hai role này. `smoke` giữ nguyên phạm vi round 1, gồm chẩn đoán của mọi episode smoke, để báo cáo smoke không đổi.
- **`role_template_support` (private, `pilot`):** một hàng cho mỗi cặp `(role, template)`, sắp theo role rồi template, gồm `episode_count`; `transaction_count` là giao dịch của chính episode trong cửa sổ quan sát; `label_snapshot_state_counts` là bốn trạng thái nhãn của giao dịch của chính episode tại mốc nhãn của episode (mốc = giao dịch cuối + `max(H, delay tối đa)` ngày; không tính lịch sử `base`); `reported_label_count` = `confirmed_fraud` + `confirmed_legit`; `truth_positive_count`, `truth_reported_count`; `incident_truth_counts_by_cause`. Số episode phải khớp allocation đã khóa của profile.
- **Kiểm tra validator:** `report_truth_boundary` (có mặt, đúng contract, không rò truth); `p2_handoff_support` (positive và negative của fit, validation, calibration, và reference cohort; thiếu thì chặn bàn giao tập đó, lý do nằm trong `missing_support`, không đổi split và không dùng truth bù); với `pilot` thêm `pilot_heldout_report_contract` (phạm vi chẩn đoán, số đếm role × template, và không có khóa `auc`/`score`/`metric`/`alert`/`signal`/`diagnos` nằm dưới ID episode `calibration` hoặc `test` ở cả hai báo cáo; chỉ đọc tên khóa, không đọc giá trị).

## 5. Bảng truth sự cố

Injector ghi ngay khi chèn sự cố; chỉ evaluator đọc.

| Cột | Ý nghĩa |
|---|---|
| `incident_truth_id`, `episode_id` | |
| `cause`, `requires_intervention` | `requires_intervention` = fraud hoặc lỗi pipeline có tác động theo cấu hình |
| `mechanism`, `rule_covered` | Phục vụ tách known/unknown |
| Phạm vi: entity, segment, feature bị tác động (trực tiếp và gián tiếp dự kiến), khoảng thời gian | Dùng cho ghép đáp án |
| `effect_available_at` | `available_at` của giao dịch chèn đầu tiên |
| Đối chiếu hiệu ứng ở P1 smoke | Injector lưu các `txn_id` bị tác động trong `run_metadata` private với key `p1.6.incident_effect.v1:<incident_truth_id>`. Validator đọc lại đúng các giao dịch operational đó, tính lại `effect_start_at = min(event_at)`, `effect_available_at = min(available_at)` và `effect_end_at = max(event_at)` nếu truth có mốc kết thúc; mọi dòng phải thuộc episode và nằm trong cửa sổ tương lai đã cấu hình. ID tham chiếu này chỉ có trong private store. |
| `intervention_start_at` | = `effect_available_at` |
| `deadline_at` | `intervention_start_at + D`; D = 7 ngày (MVP), sensitivity 3/14 |
| `accepted_actions` | Theo bảng dưới |
| `parent_scenario` | Liên kết hai sự cố thành phần của episode hai nguyên nhân; NULL nếu không có nhóm cha, còn giá trị khác NULL phải là text không rỗng. Khi mọi ứng viên của một incident cùng giá trị khác NULL thì ghép `matched` nhiều truth ([evaluation.md](evaluation.md), mục 2) |

| Nguyên nhân | `accepted_actions` | Ghi chú |
|---|---|---|
| `fraud_surge` | `fraud_ops`, `human_review` | |
| `new_fraud` | `human_review`, `fraud_ops` | Đề xuất retrain ghi kèm, không chấm |
| `data_pipeline` | `data_ticket`, `human_review` | |
| Benign (`seasonal`, `legit_business_change`, `no_material_change`) | — | Không cần can thiệp; đóng đúng tính benign automation |
| Hai nguyên nhân | Mỗi sự cố thành phần theo bảng trên | `human_review` thỏa cả hai |

Injector merchant mới ít giao dịch nhận `truth_cause` tường minh (`new_fraud` hoặc `no_material_change`); số giao dịch không dùng để suy ra nguyên nhân. Theo quyết định người dùng 01/11/2026 tại [MASTER_PLAN.md](../MASTER_PLAN.md) mục 7, pilot dùng `no_material_change` với `requires_intervention = 0`, `accepted_actions = []`; nhánh `new_fraud` được giữ nhưng không dùng trong pilot. Cả hai nhánh có `rule_covered = 0` vì nhóm merchant mới không có episode development để viết rule/ngưỡng.

Truth `legit_business_change` có `rule_covered = 0`: triage MVP không có runbook cho trường hợp này ([triage.md](triage.md) §1, §3). `silent_concept_drift` luôn mang cause `new_fraud` và mechanism `silent_concept_drift`. Cặp hai nguyên nhân được hỗ trợ là một truth `fraud_surge` và một truth `seasonal` cùng `parent_scenario`, với hai cửa sổ hiệu ứng giao nhau.

Seasonal, silent-drift và sparse-merchant rows lấy merchant theo `supplier_merchants` khi customer là household business và supplier hiện hữu phù hợp. Sparse-merchant cố ý dùng customer cá nhân để merchant mới không phá quan hệ supplier đã biết. Giao dịch injector đủ điều kiện được lấy mẫu refund/reversal theo đúng xác suất lifecycle nền tại `configs/simulator.yaml`.

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
| `event_at` | `TEXT` UTC | Bằng `as_of_time`; thời điểm nghiệp vụ của snapshot metric. |
| `available_at` | `TEXT` UTC | Bằng `as_of_time`; thời điểm snapshot metric được đọc. |
| `business_date` | `TEXT` | Ngày nghiệp vụ `Asia/Ho_Chi_Minh`, dạng `YYYY-MM-DD`. |
| `feature` | `TEXT` | Feature P2.2, `txn_count` đối với metric volume, hoặc `decision_region` đối với tỉ lệ review. |
| `segment` | `TEXT` | `all` ở P3.1; metric review ghi `model_version=<id>` để không gộp các phiên bản model. |
| `value` | `REAL NULL` | PSI cho feature, tỉ lệ review trên số model score trong cửa sổ, hoặc độ lệch tương đối của volume ngày so với median daily count của reference. NULL khi không có nền hợp lệ. |
| `reference_window` | `TEXT` | Khoảng ngày nền cố định cho PSI (`YYYY-MM-DD/YYYY-MM-DD`, hai đầu gồm); `not_applicable` cho tỉ lệ review thô vì P2.5 không lưu score của base; JSON phương pháp `reference_median_1d_count` và kỳ vọng `business_calendar` cho volume. |
| `window_start` | `TEXT` | Ngày đầu cửa sổ nghiệp vụ; PSI/review dùng cửa sổ 7 ngày, volume dùng đúng ngày `business_date`. |
| `window_end` | `TEXT` | Ngày cuối cửa sổ nghiệp vụ, gồm cả ngày này. |
| `n_window` | `INTEGER` | Số feature observation đã quan sát (gồm giá trị thiếu ở PSI), model score trong cửa sổ, hoặc số giao dịch ngày của metric volume. |
| `n_reference` | `INTEGER` | Số feature observation của base (gồm giá trị thiếu), tổng giao dịch nền, hoặc 0 cho tỉ lệ review. |
| `flags` | `TEXT` JSON array | Bộ cờ chất lượng: `window_incomplete`, `small_window_sample`, `small_reference_sample`, `no_window_samples`, `no_reference_samples`, `window_censored_samples=<số>`, `reference_censored_samples=<số>`, `window_missing_samples=<số>`, `reference_missing_samples=<số>`, `calendar_expectation_unavailable`, `target_unavailable_at_decision=<số>`, `reference_targets_unavailable_at_decision=<số>`. Không thay các cờ này bằng giá trị mặc định im lặng. |

P3 tính PSI cho feature có cờ quan sát tương ứng của P2.2: `txn_count_24h` dùng `history_window_observed_24h`, `amount_avg_7d` dùng `history_window_observed_7d`. Mẫu có cờ bằng 0 bị loại khỏi phân phối PSI ở cả cửa sổ lẫn reference; `n_window` và `n_reference` đếm mẫu còn lại sau lọc. Feature value NULL/không hữu hạn sau khi đã quan sát được giữ thành một bin thiếu riêng, không bị âm thầm loại khỏi PSI; các cờ censored và missing tách biệt. Đây là các cờ do P2.2 tính; P3 không tự suy ra lại tuổi lịch sử. `window_incomplete` tiếp tục đánh dấu cửa sổ 7 ngày chưa đủ ngày quan sát kể từ T0.

Metric `volume.txn_count` dùng feature `txn_count`, segment `all`, đếm giao dịch của episode có `event_at` thuộc đúng ngày nghiệp vụ và `available_at <= as_of_time`. `value` là độ lệch tương đối của số đếm ngày so với median daily count của base trong reference 11/2025–1/2026; nền tính cả ngày zero và không điều kiện theo thứ. `reference_window` là JSON chứa khoảng nền, phương pháp `reference_median_1d_count` và `business_calendar_expected_relative_change` tổng hợp từ `expected_effect_json`/`scope_json` trên median channel phù hợp. Volume được miễn `minimum_sample_count`; PSI metrics mới bị chặn bởi ngưỡng mẫu này. Snapshot volume vẫn tuân thủ cùng `read_as_of` và không đọc truth.

Khóa logic là `(metric_id, episode_id, as_of_time, feature, segment)` và cũng là khóa chính SQLite. Mọi nguồn dữ liệu temporal để tính bảng này, gồm `model_scores`, phải được đọc qua `read_as_of`; dòng chỉ khả dụng sau `as_of_time` không được xuất hiện. `daily_metrics` thuộc allowlist `read_as_of`, append-only, operational và không có dữ liệu truth.

### `alerts`

`alerts` là đầu ra signal operational của P3.2. Schema logic của từng alert:

| Cột | Kiểu | Ý nghĩa |
|---|---|---|
| `alert_id` | `TEXT` | ID trung tính, tất định; SHA-256 rút gọn từ `episode_id`, `as_of_time`, `metric`, `feature`, `segment`, không chứa template hoặc truth. |
| `episode_id` | `TEXT` | Episode của dòng metric đầu vào, khác `base`. |
| `as_of_time` | `TEXT` UTC | Snapshot của `daily_metrics` đã sinh alert. |
| `metric` | `TEXT` | `metric_id` của dòng `daily_metrics`; gồm metric PSI và `volume.txn_count`. |
| `feature` | `TEXT` | Feature P3.1 tương ứng. |
| `segment` | `TEXT` | Segment P3.1 tương ứng. |
| `value` | `REAL` | PSI hoặc độ lệch tương đối quan sát được cho volume ngày. |
| `reference` | `TEXT` | `reference_window` của `daily_metrics`; với volume chứa median reference baseline và public calendar expectation. |
| `threshold` | `REAL` | PSI threshold, hoặc absolute relative-change threshold cho volume tại lúc sinh alert. Volume dùng `abs(value - business_calendar_expected_relative_change)` (expected = 0 nếu không có calendar); cả hai phép so sánh đều nghiêm ngặt `>`. |
| `event_at` | `TEXT` UTC | Bằng `as_of_time` của dòng metric. |
| `available_at` | `TEXT` UTC | Bằng `as_of_time` của dòng metric; alert được xem là khả dụng tại snapshot đó. |

Khóa chính SQLite là `alert_id`; mỗi alert ứng với tối đa một dòng `(episode_id, as_of_time, metric, feature, segment)`. `alerts` thuộc allowlist `read_as_of`, append-only, operational và không có truth. CSV vẫn là đầu ra của CLI; cùng các dòng này được lưu trong DB operational để P4 đọc qua `read_as_of`.

### `incidents`

`incidents` là các snapshot operational của trạng thái gộp alert cho một episode. Mỗi thay đổi trạng thái hoặc danh sách alert tạo một snapshot mới; dữ liệu truth, template và metadata injector không được đọc hoặc lưu ở đây. `incident_id` là ID trung tính, tất định, dẫn xuất từ episode, alert đầu tiên, feature/segment và parent nếu có; không mã hóa truth. `alert_ids_json` là JSON array theo thứ tự `as_of_time`, rồi `alert_id` để phân xử các thời điểm bằng nhau. Alert đầu vào thuộc đúng episode và chỉ khả dụng nếu `available_at <= as_of_time`.

| Cột | Kiểu | Ý nghĩa / ràng buộc |
|---|---|---|
| `incident_id` | `TEXT` | ID ổn định cho một incident; không chứa template hoặc truth. |
| `parent_incident_id` | `TEXT NULL` | ID incident cha cùng episode; NULL với incident độc lập. |
| `episode_id` | `TEXT` | Episode operational, khác `base`. |
| `alert_ids_json` | `TEXT` JSON array | Danh sách alert đã gộp vào snapshot này. |
| `feature` | `TEXT` | Feature của alert/incident. |
| `segment` | `TEXT` | Segment của alert/incident. |
| `opened_at` | `TEXT` UTC | `as_of_time` của alert đầu tiên trong incident. |
| `status` | `TEXT` | `open`, `recheck`, hoặc quyết định cuối `close`, `data_ticket`, `fraud_ops`, `human_review`. `recheck` không phải trạng thái cuối. |
| `event_at` | `TEXT` UTC | Thời điểm sự kiện làm thay đổi snapshot: `as_of_time` của alert hoặc `decision_at` của quyết định. |
| `available_at` | `TEXT` UTC | Thời điểm snapshot khả dụng; lấy từ alert khi alert làm thay đổi snapshot, hoặc bằng `decision_at` khi quyết định làm thay đổi trạng thái. |

Khóa chính SQLite là `(incident_id, event_at)`; các cập nhật cùng incident tại cùng thời điểm được gộp thành một snapshot tất định. Bảng append-only và thuộc allowlist `read_as_of` để giữ trạng thái theo thời gian; truy vấn tại `as_of_time` chỉ đọc `available_at <= as_of_time` và đúng `episode_id`, rồi chọn snapshot mới nhất theo `event_at` cho mỗi `incident_id`. `parent_incident_id` tham chiếu ID ổn định trong cùng episode, không tham chiếu một phiên bản snapshot cụ thể. Bảng không chứa quyết định gốc hay thay đổi chúng; P4 cung cấp quyết định qua interface của monitoring.

### Báo cáo evaluator P3.4 (private)

Evaluator ghi JSON tại `reports/p3_4_detection_recall.json` tương đối với thư mục private DB. Công thức và cách diễn giải metric thuộc [evaluation.md](evaluation.md); bảng dưới đây định nghĩa riêng cấu trúc báo cáo.

| Trường | Kiểu | Ý nghĩa |
|---|---|---|
| `schema_version` | `INTEGER` | Phiên bản cấu trúc JSON; P5.2 Fix 1 dùng phiên bản `3`. |
| `as_of_time` | `TEXT` UTC | Cutoff dùng để lọc snapshot incident theo `available_at`. |
| `matching_scope` | `TEXT` | Phạm vi gồm effect window cộng metric window, feature lookback, segment và feature. |
| `metric_window_days` | `INTEGER` | Độ dài metric window lấy từ cấu hình run nếu có, đối chiếu với `configs/monitoring.yaml` public. |
| `feature_lookback_days` | `OBJECT` JSON | Lookback theo ngày của các feature monitoring, lấy từ định nghĩa public trong `model/features.py`. |
| `deferred_to_round2` | `TEXT[]` JSON | Trường tương thích; rỗng khi matcher P5.2 đã hoàn tất. |
| `feature_mapping` | `OBJECT` JSON | Mapping tường minh từ feature monitoring sang feature truth. |
| `detection_recall` | `OBJECT` JSON | Tổng số sự cố cần can thiệp được phát hiện. |
| `by_scenario` | `OBJECT[]` JSON | Số đếm theo cặp `cause`/`template`; `recall` là NULL khi mẫu số bằng 0. |
| `truth_counts` | `OBJECT` JSON | Tổng số truth, truth cần can thiệp và truth benign trong episode đã chọn. |
| `incident_counts` | `OBJECT` JSON | Tổng số incident đã xét, matched, ambiguous, unmatched và matched với truth benign. |
| `ambiguous_incident_ids` | `TEXT[]` JSON | Incident có hơn một truth ứng viên, trừ ca mọi ứng viên cùng `parent_scenario` khác NULL (khi đó `matched`); cách tính xem [evaluation.md](evaluation.md), mục 2. |
| `unmatched_incident_ids` | `TEXT[]` JSON | Incident không có truth ứng viên; cách tính xem [evaluation.md](evaluation.md), mục 2. |
| `benign_incident_ids` | `TEXT[]` JSON | Incident `matched` chỉ khi mọi ID trong `matched_truth_ids` đều là truth benign; incident có truth cần can thiệp đồng thời không được tính. |
| `incident_matches` | `OBJECT[]` JSON | Trạng thái, truth ID được xác nhận và truth ID ứng viên cho từng incident. |

Các object con có cấu trúc cố định: `detection_recall` gồm `detected`, `denominator`, `recall` (`REAL NULL`) và `missed_truth_ids`; mỗi phần tử `by_scenario` gồm `cause`, `template`, `detected`, `denominator`, `missed_truth_ids`, `recall` (`REAL NULL`) và `benign_truth_count`; `truth_counts` gồm `total`, `requires_intervention`, `benign`; `incident_counts` gồm `evaluated`, `matched`, `ambiguous`, `unmatched`, `benign_matched`; mỗi phần tử `incident_matches` gồm `incident_id`, `episode_id`, `feature`, `segment`, `incident_start_at`, `incident_end_at`, `status`, `matched_truth_ids`, `candidate_truth_ids`. `status` là `matched`, `ambiguous_match` hoặc `unmatched`; `matched_truth_ids` chỉ chứa truth ID khi status là `matched`, còn `candidate_truth_ids` liệt kê mọi truth tương thích. `benign_matched` và `benign_incident_ids` chỉ đếm incident có danh sách `matched_truth_ids` không rỗng mà toàn bộ truth trong danh sách đều benign. Quy tắc scoring và workload thuộc [evaluation.md](evaluation.md), mục 2. `incident_start_at`/`incident_end_at` là mốc `available_at` sớm nhất/muộn nhất của các snapshot có thêm alert ID vào `alert_ids_json`; snapshot quyết định không mở rộng khoảng này. Timestamp dùng UTC; các mảng ID giữ thứ tự tất định.

## 8. Đầu ra triage (`decisions`)

`decisions` là bảng operational ghi một dòng cho mỗi lần gate chạy, kể cả khi kết quả là `recheck`. Bảng chỉ chứa kết luận triage, checklist/tool output và chi phí thực thi; không đọc hoặc lưu truth, template, cơ chế injector hay metadata private.

**Nguồn khi báo cáo (người dùng chốt 31/10/2026, audit B5):** schema không đổi; mọi report tách theo nguồn coi `source='agent' AND model_slug IS NULL` (do `UnconfiguredAgent` của baseline rule + human) là **chuyển người (baseline)**, không tính vào nhánh agent. Nhánh agent chỉ gồm `source='agent'` có `model_slug` khác NULL.

| Cột | Kiểu | Null? | Ý nghĩa / ràng buộc |
|---|---|---|---|
| `decision_id` | `TEXT` | Không | ID trung tính, tất định, SHA-256 rút gọn từ ngữ cảnh quyết định operational; không chứa truth. |
| `incident_id` | `TEXT` | Không | Incident snapshot được gate xử lý. |
| `episode_id` | `TEXT` | Không | Episode operational, khác `base`. |
| `decision_at` | `TEXT` UTC | Không | Bằng `as_of_time` của snapshot theo quyết định đã chốt. |
| `as_of_time` | `TEXT` UTC | Không | Cutoff mà gate dùng để xác thực snapshot. |
| `event_at` | `TEXT` UTC | Không | Bằng `decision_at`; timestamp sự kiện temporal để đọc qua `read_as_of`. |
| `available_at` | `TEXT` UTC | Không | Bằng `decision_at`; quyết định khả dụng ngay tại cutoff của snapshot. |
| `source` | `TEXT` | Không | `rule` hoặc `agent`. |
| `runbook` | `TEXT` | Có | Runbook đề xuất; NULL nếu không áp dụng. |
| `cause` | `TEXT` | Không | Nguyên nhân ứng viên từ rule/agent; không thay `evidence_status`. |
| `evidence_status` | `TEXT` | Không | `sufficient`, `insufficient`, hoặc `conflicting`; do code tính từ checklist/tool output và tính hợp lệ của snapshot. |
| `action` | `TEXT` | Không | `close`, `recheck`, `data_ticket`, `fraud_ops`, hoặc `human_review` sau gate. |
| `scope_json` | `TEXT` JSON object | Không | Phạm vi incident gồm `feature`, `segment`, `start_at`, `end_at`; `start_at` lấy từ `opened_at`, `end_at` bằng `as_of_time`. |
| `checklist_results_json` | `TEXT` JSON object | Không | Kết quả từng checklist item đã chuẩn hóa; không nhận `evidence_status` từ agent. |
| `tool_trace_ids_json` | `TEXT` JSON array | Không | Các trace ID operational theo thứ tự được cung cấp; `[]` nếu không có. |
| `conclusion` | `TEXT` | Không | Tóm tắt cause đề xuất, action đề xuất, action sau gate và evidence status. |
| `reason` | `TEXT` | Không | Lý do cho action sau gate, gồm lý do chờ nếu action là `recheck`. |
| `close_score` | `REAL` | Có | Score agent do code cung cấp, trong `[0, 1]`; NULL nếu không có score. Rule không ghi score. |
| `policy_version` | `TEXT` | Không | Phiên bản policy dùng cho lần gate. |
| `model_slug` | `TEXT` | Có | Canonical slug của model agent đã cấu hình cho lượt điều tra; NULL với rule (validator bắt buộc) hoặc khi agent không cấu hình. |
| `input_tokens` | `INTEGER` | Có | Token đầu vào nếu có; không âm. |
| `output_tokens` | `INTEGER` | Có | Token đầu ra nếu có; không âm. |
| `total_tokens` | `INTEGER` | Có | Tổng token nếu có; không âm. |
| `cost_usd` | `REAL` | Có | Chi phí USD nếu có; không âm. |

Khóa logic là `decision_id`; cùng ngữ cảnh và kết quả gate cho ID giống nhau, kết quả khác tạo ID khác. Các trường JSON phải parse đúng kiểu đã nêu. `decisions` là bảng operational append-only, có khóa bất biến như các bảng temporal operational khác; thuộc allowlist `read_as_of` và chỉ chứa episode khác `base`. `event_at = available_at = decision_at = as_of_time`, nên quyết định chỉ xuất hiện khi cutoff đạt thời điểm snapshot. Bảng không chứa truth. Evaluator được nối ở P4.5, còn module triage không import evaluator.

`scope_json` có bốn khóa `feature`, `segment`, `start_at`, `end_at`; giá trị là TEXT hoặc NULL khi snapshot lỗi không cung cấp được phạm vi. `checklist_results_json` là object ánh xạ tên checklist sang object kết quả; mỗi kết quả có `status` ∈ `pass`, `fail`, `pending`, `conflicting`, `error`, `not_applicable`, và có thể có `reason`, `scope_match`, `window_complete`, `requires_human_review`, `details`. Ba cờ là BOOLEAN; `details` là JSON. `evidence_status` không phải đầu vào checklist được lưu. `tool_trace_ids_json` là mảng các TEXT ID không rỗng.

`__runner_guard` là mục dành riêng cho code trong `checklist_results_json`; không nhận từ agent. Khi lỗi tool ở runner hoặc snapshot không hợp lệ buộc gate chuyển người, mục này có `status = "not_applicable"` và `details = {"forced_human": true, "reason_codes": [...]}`. Evaluator support coi cờ này là điều kiện forced-human.

### Báo cáo evaluator P4.5 (private)

Evaluator ghi `reports/p4_5_decisions.json` tương đối với thư mục private DB. Báo cáo dùng `schema_version = 3`; `matching_scope` nêu decision-scope, effect window, metric window, feature lookback, segment và feature. `metric_window_days` lấy từ cấu hình run nếu có và được đối chiếu với `configs/monitoring.yaml`; `feature_lookback_days` lấy từ `model/features.py`.

| Trường top-level | Cấu trúc |
|---|---|
| `schema_version` | `INTEGER`, hiện là `3`. |
| `matching_scope` | `TEXT`. |
| `metric_window_days` | `INTEGER`. |
| `feature_lookback_days` | `OBJECT` ánh xạ feature sang số ngày lookback. |
| `episode_ids` | `TEXT[]` JSON, thứ tự tăng dần. |
| `selection` | Object gồm `policy_version`, `model_slug`, `excluded_decision_count`. |
| `decision_counts` | Object xem cấu trúc bên dưới. |
| `truth_counts` | Object gồm `total`, `requires_intervention`, `benign`. |
| `timely_routing_recall` | Object xem cấu trúc bên dưới. |
| `unsafe_truth_rate` | Object xem cấu trúc bên dưới. |
| `dangerous_closure_rate` | Object xem cấu trúc bên dưới. |
| `decision_matches` | Mảng object ghép từng decision với status, matched IDs và candidate IDs. |

Các object con có cấu trúc cố định:

- `decision_counts`: `total`, `matched`, `unmatched`, `ambiguous`, `close`, `unmatched_closures`, `ambiguous_closures`, `unmatched_closure_decision_ids`, `ambiguous_closure_decision_ids`. Hai trường `*_closure_decision_ids` là `TEXT[]` JSON đã sắp xếp.
- `unsafe_truth_rate`: `unsafe`, `denominator`, `rate` (`REAL NULL`), `unsafe_truth_ids`, `lower_bound_observed`, `basis`. `basis` là `observed_lower_bound`, `all_candidate_truths_included` hoặc `matched_truth_only`.
- `timely_routing_recall`: `routed`, `denominator`, `recall` (`REAL NULL`), `truth_ids`, `missing_truth_ids`, `automatic_routing`, `human_review`. Hai bucket routing gồm `routed`, `denominator`, `recall` (`REAL NULL`), `truth_ids`.
- `dangerous_closure_rate`: `dangerous_closures`, `denominator`, `rate` (`REAL NULL`), `dangerous_incident_ids`, `triaged_incident_ids`.
- Mỗi `decision_matches` gồm `decision_id`, `incident_id`, `episode_id`, `decision_at`, `status`, `matched_truth_ids`, `candidate_truth_ids`. `status` là `matched`, `ambiguous_match` hoặc `unmatched`; `matched_truth_ids` chỉ chứa truth được ghép (một ID, hoặc nhiều ID khi ca cùng `parent_scenario` khác NULL), `candidate_truth_ids` giữ mọi ứng viên. Quy tắc scoring thuộc [evaluation.md](evaluation.md), mục 2–4.

Một run directory chỉ dùng một agent provider. Runner từ chối chạy nếu DB đã có quyết định `source='agent'` với `model_slug` khác provider đang chọn (bao gồm `NULL` của `unconfigured`); hãy dùng bản sao mới của run directory cho mỗi provider.

## 9. P5.1 harness artifacts

Các file dưới đây nằm ngoài SQLite. Chúng không chứa truth, injector data, seed, prompt body, hay SQL; `seed-role-ledger.json` chỉ ghi benchmark root, episode ID và role operational đã được chạm. BASE/policy manifests và reports nằm trong benchmark root. Cost ledger, role ledger và lock markers dùng chung một state directory cố định ngoài mọi benchmark root và operational DB mà triage/tool đọc. Mặc định là `~/.payguard-benchmark/`; `PAYGUARD_BENCHMARK_STATE_DIR` là override tuyệt đối chỉ dành cho tests. Harness ghi đường dẫn đã resolve vào `harness-manifest.json` và từ chối resume, triage hoặc evaluator nếu vị trí hiện tại khác; benchmark root cũng không được nằm bên trong state directory.

| File | Cấu trúc / ràng buộc |
|---|---|
| `harness-manifest.json`, `base/generation-manifest.json` | JSON object: `schema_version = 1`, `profile` theo simulator (`smoke` hoặc `pilot`); harness manifest lưu `state_dir` đã resolve. Root có profile khác (smoke ↔ pilot) bị từ chối khi resume; BASE và generation manifest cũng phải khớp profile. `hashes` gồm Git HEAD/dirty flag dạng thông tin và SHA-256 cho runtime code, configs (gồm `configs/benchmark.yaml`), prompt sources, sampling. Khi resume harness hoặc triage policy run, state directory và các hash nội dung runtime phải khớp; Git HEAD/dirty không chặn resume. |
| `base/base-manifest.json` | JSON object: schema/profile, `freeze_point`, `hashes`, `input_fingerprint` SHA-256, `model_artifacts`, và số đếm pipeline. Fingerprint canonical của mọi bảng operational, coi `incidents` và `decisions` là rỗng. `model_artifacts` content-hash các file `predictor.joblib`, `calibrator.joblib`, `conformal.joblib`, `metadata.json` thực dùng; metadata gồm phiên bản và provenance model. BASE freeze sau `model_scores`, `daily_metrics`, `alerts`, trước hai bảng này. |
| `policies/<name>/run-manifest.json` | JSON object: schema, `policy_name`, `provider_name`, `policy_version`, `model_slug` (có thể NULL), `hashes`, `model_artifacts` giống BASE, `base_fingerprint`, `run_fingerprint`. Resume so các hash nội dung runtime, danh tính artifact model, identity policy và fingerprint BASE; Git HEAD/dirty giữ làm thông tin. Harness evaluator còn yêu cầu hash trong run manifest khớp lock held-out, identity policy trong test lock khớp, và fingerprint operational copy của policy bằng BASE. |
| `<state-dir>/locks/<profile>/prompt-rule.json`, `<state-dir>/locks/<profile>/policy/<name>.json` | JSON object: `schema_version`, profile lock chuẩn hóa (`smoke` hoặc `benchmark`), `lock_kind`, `locked_at` UTC, `identity.hashes`; policy lock còn có tên policy, `provider_name`, `policy_version`, `model_slug`. `pilot` của simulator ánh xạ sang `benchmark`; `create_lock` từ chối profile không khớp manifest của run. Smoke và benchmark có lock độc lập; trong mỗi profile, marker bất biến và dùng chung qua mọi root. Lock hashes chỉ gồm nội dung runtime code, configs, prompt sources và sampling, không gồm Git HEAD/dirty flag. Phân tích chỉ dùng khi hash trong lock khớp cả identity hiện tại và run manifest. |
| `<state-dir>/seed-role-ledger.json` | JSON object gồm `schema_version` và `entries` keyed bởi SHA-256. Mỗi entry ghi `benchmark_root`, `code_identity_hash`, operation, policy, `roles` (role → episode ID list), usage (`analysis` hoặc `operational_execution`) và `touched_at` UTC. Root và identity hash nằm trong entry để các episode ID giống nhau của hai root/code version không bị gộp; cùng một operation/root/code identity/role/episode set chỉ có một entry khi resume. |
| `<state-dir>/cost-ledger.jsonl` | Append-only JSON Lines, mỗi event có `schema_version = 1`, `event`, `recorded_at` UTC, `amount_usd` dạng decimal text, `model_slug`, `policy`. `reserve` ghi reservation ID và `cap_usd`; mọi reservation phải dùng cùng cap đã ghi trong ledger, cap khác bị từ chối. `settle` chốt reservation và ghi cờ `conservative`, `conservative_reason`, cùng cờ `overrun` (true khi actual cost lớn hơn reservation). Số tiền settle chỉ lấy từ usage đo được: `cost` hợp lệ (số JSON hữu hạn ≥ 0), hoặc khi không có `cost` hợp lệ thì từ hai bộ đếm `prompt_tokens` và `completion_tokens` đều là số nguyên ≥ 0 nhân giá đã cấu hình. Mọi trường hợp khác giữ đúng mức reservation với `conservative = true` và `conservative_reason` thuộc `usage_missing`, `response_not_object`, `cost_invalid`, `token_usage_incomplete`, `request_failed`, `transport_error`; settlement đo được ghi `conservative = false`, `conservative_reason = null`; `fake_call` ghi 0; `blocked` ghi ước lượng/cap và nguồn chặn. Reservation chưa settle sau crash vẫn được tính vào cap. Snapshot báo `overrun`, `overrun_count`, `overrun_usd`; policy run report ghi `cost_overrun`. |

`hashes` của run có `git_head`, `git_dirty`, `code_hash`, `config_hash`/`config_hashes`, `prompt_hash`/`prompt_hashes`, và `sampling_hash`; các nội dung được băm bằng SHA-256 trên JSON canonical hoặc bytes của file. Resume, lock, và run fingerprint so các hash nội dung, giữ Git metadata để tra cứu; lỗi identity liệt kê tên hash nội dung bị lệch. `input_fingerprint` của mỗi policy phải bằng BASE trước và sau triage. Triage harness chỉ xử lý role được chọn sau khi lock tương ứng đã tồn tại; mặc định CLI standalone chỉ xử lý development, role held-out phải qua role guard và state directory chung. Policy OpenRouter `rule_agent_openrouter_k1` dùng `cost-ledger.jsonl` chung; CLI standalone chọn OpenRouter cũng dùng ledger bền với cap benchmark. Chi tiết CLI/lock workflow ở [P5 implementation](p5-implementation.md).

`model_artifacts` là object `{schema_version: 1, files, content_sha256}`. `files` ánh xạ tên bốn file model ở trên tới SHA-256 bytes; `content_sha256` là SHA-256 của JSON canonical gồm `schema_version` và `files`. Trong P5.1 harness, trước khi CLI `model evaluate` ghi role ledger hoặc đọc transaction truth, artifact directory được chọn phải khớp identity trong cả BASE và policy run manifest.

`configs/benchmark.yaml` đặt cap benchmark `cost_cap_usd.value = 10.0` USD, với provenance `MASTER_PLAN.md`, mục 5. Reservation dùng giá đã cấu hình; settlement thực tế cao hơn được ghi là overrun, nên cap trước cuộc gọi chỉ là cận trên khi provider giữ nguyên hoặc thấp hơn các giá đã cấu hình.

## 10. Báo cáo support development P5.0b (private)

Lệnh `python -m evaluation support --run <benchmark-root>/policies/<policy>` chọn episode development theo `episode_manifest.role`, đối chiếu role đó với `episodes.role` trong operational DB, kiểm tra `--out`, rồi gọi role guard P5.1 trước khi đọc truth. Mismatch role bị từ chối trước khi role ledger được ghi. Báo cáo mặc định nằm ở `base/private/reports/p5_0b/<policy>/development_support.json`; không có tùy chọn chọn role held-out. `--out` nếu dùng phải nằm dưới `base/private/reports/`. Báo cáo là pilot/feasibility theo development, không thay đổi số episode hoặc tỉ lệ 12/24/24. Seasonal benign support smoke hiện bằng 0; việc đo support này được hoãn theo quyết định P3.7 tại [bàn giao P3](p3-implementation.md).

JSON dùng `schema_version = 1`, `report_type = "p5_0b_development_support"`, `purpose = "pilot_feasibility"` và `episode_role = "development"`. Thứ tự mảng và khóa JSON ổn định để cùng input tạo cùng bytes.

| Trường top-level | Kiểu / ý nghĩa |
|---|---|
| `policy_name` | `TEXT`, policy run được phân tích. |
| `episode_ids` | `TEXT[]`, episode development đã chọn theo thứ tự tăng dần. |
| `as_of_time`, `as_of_time_by_episode` | Timestamp UTC cao nhất và cutoff UTC từng episode; bảng operational chỉ đọc với `available_at <= cutoff` và đúng episode. |
| `truth_counts` | Tổng truth, truth cần can thiệp, truth benign, số truth có `rule_covered = 1`, số đếm theo episode và phân nhóm bên dưới. |
| `incident_counts` | Số incident `matched`, `ambiguous_match`, `unmatched`, tổng số đã đánh giá và bảng cùng số đếm theo episode. |
| `window_completeness` | Cửa sổ `daily_metrics` hoàn tất/chưa hoàn tất theo metric-feature, cùng số kết quả checklist triage có `window_complete` true/false/thiếu. Metric có `window_incomplete` hoặc `window_censored_samples` được tính chưa hoàn tất. |
| `rule_coverage` | Số truth có `rule_covered = 1` / tổng truth (`coverage = null` nếu mẫu số 0), và runbook trên từng incident có quyết định `source = 'rule'`, gồm incident IDs và matched truth IDs. |
| `agent_support` | Số incident duy nhất tới agent, số decision rows agent và đếm theo episode; nhóm incident theo truth; số incident đủ điều kiện checklist cho nhánh rule/agent, quyền đóng hiện tại và điều kiện UQ P5.0a (1)–(4) theo `seasonal_calendar.close_enabled` off/on. Checklist eligibility đòi `seasonal_calendar` pass, đúng scope, cửa sổ hoàn tất, evidence đủ, và không có forced-human condition theo gate. |
| `background_workload` | Số unmatched incident/alert, alert trong unmatched incident, alert chưa được incident nào chứa, danh sách ID tương ứng, và tổng alert development duy nhất. Unmatched vẫn thuộc workload, không được tính benign. |
| `seasonal_benign_support` | Số seasonal benign truth, số seasonal benign truth được incident khớp benign hỗ trợ, số incident có seasonal benign truth được khớp benign và trạng thái deferred. Incident matched chỉ là benign-matched nếu mọi truth khớp của incident đều benign. |
| `interpretation` | Nhãn pilot/feasibility, cờ split không đổi, trạng thái và lý do runtime chưa có checklist `seasonal_calendar`, cùng ghi chú giới hạn seasonal benign support. Các số 0 về eligibility trong trạng thái này không đo support của checklist chưa được runtime sinh ra. |

Mỗi phần tử `truth_counts.by_episode` gồm `episode_id`, `unique_truths`, `requires_intervention`, `benign`, `rule_covered_truths`; danh sách có cả episode không có truth để hiện rõ số 0. Mỗi phần tử `truth_counts.by_episode_and_cause` gồm `episode_id`, `cause`, `template`, `unique_truths`, `requires_intervention`, `benign`, `matched_truths`, `rule_covered_truths`, `matched_runbooks`. Các truth được đếm theo ID duy nhất; một runbook chỉ được gắn với truth khi incident có match rõ và có decision rule lưu runbook đó. Mỗi phần tử `incident_counts.by_episode` gồm `episode_id`, `evaluated`, `matched`, `ambiguous_match`, `unmatched`.

`window_completeness.daily_metric_snapshots` gồm `evaluated`, `complete`, `incomplete` và `by_metric` (`metric_feature`, `complete`, `incomplete`). `window_completeness.triage_checklists` gồm từng `checklist` với số `complete`, `incomplete`, `unknown` trên decision rows. `rule_coverage.matched_runbooks` gồm `runbook`, `incident_count`, `incident_ids`, `matched_truth_ids`.

`agent_support.by_truth_group` là mảng theo `episode_id` và nhóm incident tới agent. Nhóm `group_type = "truth"` có `cause`, `template`, `has_parent_scenario`; nhóm `ambiguous` và `unmatched_background` có ba trường này bằng NULL. Mỗi nhóm ghi `incidents_reaching_agent` và điều kiện (1)–(4) theo trạng thái `close_enabled` off/on. Có một hàng cho mọi nhóm truth development trong episode, kể cả khi không có incident matched hoặc incident nào tới agent, và hai hàng đặc biệt cho từng episode. Incident matched nhiều truth cùng parent có mặt trong từng nhóm truth tương ứng, nên số đếm giữa nhóm có thể chồng lấn và không được cộng thành tổng.

`agent_support.close_eligibility.rule_path` gồm `checklist_eligible_incidents`, `close_enabled`, `authorized_to_close_now`; `agent_path` gồm `checklist_eligible_incidents`, `agent_close_disabled`, `tau_agent`, `authorized_to_close_now`. `agent_support.uq_conditions` gồm số incident thỏa (1)–(3), cờ quyền đóng agent theo điều kiện (4), tổng số thỏa (1)–(4), `agent_close_disabled`, và `by_close_enabled` với hai khóa `off`/`on`. Mỗi trạng thái có `conditions_1_to_3_incidents`, `conditions_1_to_4_incidents`, `conditions_1_to_3_reason`, `conditions_1_to_4_reason`; count không đo được là NULL và phải có reason. Mỗi hàng `by_truth_group` có cùng hai trạng thái và bốn trường tương ứng; trạng thái aggregate không đo được thì nhóm cũng NULL kèm reason, không ghi thành số 0. Nếu một incident tới agent không có checklist mùa vụ đã lưu, trạng thái bị NULL kèm lý do thay vì coi thiếu dữ liệu là số 0. Khi `seasonal_calendar` bật, ca đạt checklist trên cùng phạm vi được rule đóng trước chỉ khi thời điểm `decision_at` của rule không muộn hơn thời điểm quyết định của agent; phạm vi so sánh chỉ gồm `feature` và `segment`, không gồm thời gian scope. Nếu run hiện tại không chứa rule-side checklist hoặc scope để đối chiếu, trạng thái đó là NULL thay vì suy diễn số. Với cấu hình vòng 1 hiện tại, agent close bị tắt nên điều kiện (4) và tổng support (1)–(4) bằng 0; số (1)–(3) vẫn được báo độc lập.

`interpretation.seasonal_calendar_checklist_available` là FALSE ở runtime vòng 1; `seasonal_calendar_checklist_availability_reason` ghi lý do: runtime chỉ có checklist `unit_or_null_change`. Vì vậy các số 0 về eligibility/conditions ở các run không có checklist mùa vụ là số đếm trên quyết định đã lưu, không phải bằng chứng rằng development không có ca hỗ trợ checklist.

`background_workload` gồm `unmatched_incidents`, `unmatched_incident_ids`, `unmatched_alerts`, `unmatched_alert_ids`, `alerts_in_unmatched_incidents`, `unassigned_alerts`, `all_development_alerts`. Các danh sách ID được sắp xếp; số alert unmatched là hợp của alert ID trong snapshot cuối của incident unmatched và alert không xuất hiện trong snapshot incident nào.
