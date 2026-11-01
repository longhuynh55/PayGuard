# P1 — Sub-plan triển khai simulator và incident injector

> Trạng thái: **đang triển khai; P1.1–P1.6 vòng 1 đã có runtime**. Phạm vi và mốc P1: [MASTER_PLAN.md](../MASTER_PLAN.md).
> File này sở hữu **thứ tự công việc, artifact và điều kiện nghiệm thu P1**. Schema/chính sách dữ liệu vẫn chỉ định nghĩa trong [data.md](data.md); không chép schema vào đây.

## 1. Kết quả cần bàn giao

Từ config và seed, sinh lại được một bộ dữ liệu gồm thế giới nền, giao dịch, nhãn trễ, lịch nghiệp vụ và các episode có sự cố; xuất dữ liệu vận hành tách khỏi truth; có báo cáo kiểm định và đầu vào đủ cho P2/P3.

P1 hoàn thành khi các điều kiện nghiệm thu ở mục 6 đạt. Model, monitoring thực tế, rule/agent và UI thuộc các stage sau; không dùng PR-AUC hoặc việc model phải bỏ sót một kiểu fraud làm tiêu chí đạt P1.

### Tài liệu làm căn cứ

| Nội dung | Nguồn chính |
|---|---|
| Nghiệp vụ simulator, schema, as-of, nhãn, lịch, episode, truth | [data.md](data.md) |
| Timeline và support cần audit cho P2 | [model.md](model.md) |
| Vai trò episode, manifest và các trường truth phục vụ đánh giá | [evaluation.md](evaluation.md) |
| Dữ liệu quan sát mà runbook cần | [triage.md](triage.md) |
| Phạm vi MVP/mở rộng và ngân sách | [MASTER_PLAN.md](../MASTER_PLAN.md) |

Nếu phải cụ thể hóa type, khóa, đơn vị, nullability hoặc semantics của một cột, cập nhật tại `data.md` trước rồi hiện thực trong code. Nếu phát hiện cần thay đổi protocol, cập nhật ví dụ review và nhật ký theo [AGENTS.md](../AGENTS.md).

## 2. Format dùng cho mỗi task

```text
ID / trạng thái:
Mục tiêu quan sát được:
Phụ thuộc / đầu vào:
File tạo hoặc sửa:
Các bước triển khai:
Kiểm tra và kết quả mong đợi:
Điều kiện hoàn tất:
```

Trạng thái task: `todo` → `in_progress` → `round1-done` → `done`, hoặc `blocked` kèm nguyên nhân cụ thể. Chỉ đánh dấu done khi có kết quả chạy kiểm tra. Bằng chứng chạy đặt trong artifact báo cáo; khi code được tạo thì link tới file/lệnh thực tế thay vì giữ mô tả chung.

## 3. Bố trí file dự kiến

Đây là đường dẫn **sẽ tạo khi triển khai**, hiện chưa phải code sẵn có. Có thể gộp module nhỏ nếu vẫn giữ đúng ranh giới dữ liệu và các phép kiểm tra.

```text
configs/
  simulator.yaml       # tham số, đơn vị, provenance, seed, profile smoke/pilot
  episodes.yaml        # định nghĩa kịch bản và phân vai episode
simulator/
  __init__.py
  __main__.py          # CLI generate / validate
  config.py
  schema.py
  storage.py           # xuất operational và private artifacts
  asof.py
  world.py
  transactions.py
  fraud.py
  labels.py
  incidents.py
  episodes.py
  validate.py
tests/
  test_simulator_integrity.py
  test_simulator_asof.py
  test_simulator_labels.py
  test_simulator_incidents.py
  test_simulator_episodes.py
  test_simulator_reproducibility.py
data/                  # dữ liệu sinh lại, không đưa vào git
  <run>/
    operational/       # chỉ dữ liệu đủ điều kiện cấp cho model/monitoring/tools
    private/           # truth và thông tin nội bộ của generator/evaluator
    reports/           # integrity, support, provenance, fingerprint, runtime
```

Tên `operational`/`private` chỉ là bố trí artifact, không tự tạo quyền truy cập. Connection/tool về sau phải giới hạn đúng nguồn operational; không cấp nguyên thư mục run cho agent. P1 kiểm tra public schema/DB không chứa truth; P4 phải kiểm tra tiếp giới hạn truy cập từ tool thực tế.

## 4. Các task triển khai

### Thứ tự: hai vòng (chốt 25/10/2026)

Task giữ nguyên P1.1–P1.8; mỗi task làm theo **hai vòng**. Vòng 1 làm tối thiểu để đi tới mốc ★ luồng mỏng trong [MASTER_PLAN.md](../MASTER_PLAN.md) (P1 → P2 → P3 tối thiểu); vòng 2 làm sâu sau ★. Điều kiện hoàn tất ghi trong từng task là của **vòng 2**; vòng 1 chỉ cần phần ghi dưới đây.

| Task | Vòng 1 — luồng mỏng | Vòng 2 — sau ★ |
|---|---|---|
| P1.1 Config | Đầy đủ | — |
| P1.2 Schema, storage, as-of (`round1-done`) | Đầy đủ | — |
| P1.3 Thế giới, giao dịch hợp lệ | Tối thiểu: entity + giao dịch hợp lệ có nhịp ngày, profile smoke | Đủ kênh, vai trò tài khoản, refund/reversal |
| P1.4 Fraud | **Một** cơ chế (card testing) | Đủ cơ chế, social engineering, cash-out, `legit_business_spike` |
| P1.5 Nhãn | Đầy đủ | — |
| P1.6 Injector, calendar, truth | 1–2 template: `data_pipeline` (đổi đơn vị) + `seasonal` một ngày; truth đủ cột | Các template còn lại, ca hai nguyên nhân |
| P1.7 Episode, manifest | Smoke: vài episode, manifest có vai trò | Pilot 60 episode |
| P1.8 CLI, validation | `generate`/`validate` cho smoke | Pilot, báo cáo support đầy đủ |

Vòng 1 xong khi P2 và P3 tối thiểu đọc được artifact smoke; trạng thái task ghi `round1-done` trước khi lên `done`.


### P1.1 — Cấu hình và provenance (`round1-done`)

- **Mục tiêu:** mọi tham số được sử dụng có giá trị, đơn vị, lý do và nguồn hoặc nhãn giả định rõ ràng.
- **Phụ thuộc / đầu vào:** các nguồn đã chọn trong `data.md` / [rationale.md](rationale.md); phạm vi MVP và các vai trò episode hiện hành.
- **File:** `configs/simulator.yaml`, `configs/episodes.yaml`, `simulator/config.py`; bổ sung chi tiết hợp đồng tại `data.md` nếu cần.
- **Triển khai:** định nghĩa config cho thế giới, thời gian, hành vi giao dịch, fraud, reporting delay và quy mô. Tách tham số nghiệp vụ khỏi seed và số lượng của profile smoke/pilot. Khai báo rõ tham số nào có nguồn, tham số nào là giả định mô phỏng; không gán tên nguồn cho một con số chưa được nguồn hỗ trợ. Chốt semantics của timestamp/timezone, đơn vị tiền và cách tham chiếu lịch sử chung theo episode trong `data.md`.
- **Kiểm tra:** config thiếu trường bắt buộc, probability ngoài miền, lịch thời gian không hợp lệ hoặc giá trị không có đơn vị/provenance phải được báo rõ. Profile smoke giảm quy mô, không đổi ý nghĩa của protocol.
- **Hoàn tất:** config đọc được và validate được; các giá trị chưa có nguồn mang nhãn assumption thay vì bị bỏ trống hoặc chặn vô thời hạn. Không mở thêm vòng literature review rộng.

### P1.2 — Schema, storage và fixture nhỏ (`round1-done`)

- **Mục tiêu:** có fixture thủ công rất nhỏ chạy xuyên qua bước ghi/đọc dữ liệu và snapshot as-of.
- **Phụ thuộc:** P1.1; hợp đồng dữ liệu ở `data.md` được cụ thể hóa đủ để tạo bảng.
- **File:** [`simulator/schema.py`](../simulator/schema.py), [`simulator/storage.py`](../simulator/storage.py), [`simulator/asof.py`](../simulator/asof.py), fixture [`tests/fixtures/p1_2_operational.json`](../tests/fixtures/p1_2_operational.json), test [`tests/test_simulator_integrity.py`](../tests/test_simulator_integrity.py) và [`tests/test_simulator_asof.py`](../tests/test_simulator_asof.py). Chi tiết type/khóa/nullability ở [data.md](data.md).
- **Triển khai:** tạo schema, khóa và quan hệ; export operational/private; xây API đọc theo as-of và episode. Phân loại từng trường thành dữ liệu quan sát được, nhãn đã khả dụng hoặc metadata/truth nội bộ. Vai trò fraud ẩn, ID template/seed và đáp án injector không được đưa vào feature operational. Dựng fixture gồm nhiều episode, dữ liệu về trễ và nhãn chưa về để kiểm tra ranh giới trước khi viết generator lớn.
- **Kiểm tra:** khóa trùng, FK mồ côi, truy vấn nhầm episode, bản ghi chưa khả dụng, thời điểm đúng bằng cutoff; thêm dữ liệu chỉ khả dụng sau t không đổi snapshot tại t. Liệt kê bảng/cột/export của operational để xác nhận không có truth nội bộ.
- **Hoàn tất:** fixture ghi/đọc được, trả đúng các hàng theo episode/as-of và không lẫn truth. Không coi một DB nằm ở thư mục khác là bằng chứng cô lập tool end-to-end.

### P1.3 — Thế giới và giao dịch hợp lệ (`round1-done`; vòng 2 ban đầu chưa làm; vòng 2 triển khai 29/10/2026 — xem trạng thái bổ sung bên dưới)

- **Mục tiêu:** sinh lịch sử nền nhất quán từ profile khách hàng/merchant, gồm hành vi ngày/giờ/kênh theo config.
- **Phụ thuộc:** P1.2.
- **File:** [`simulator/world.py`](../simulator/world.py), [`simulator/transactions.py`](../simulator/transactions.py), [`simulator/reproducibility.py`](../simulator/reproducibility.py), cấu hình tại [`configs/simulator.yaml`](../configs/simulator.yaml); test [`test_simulator_integrity.py`](../tests/test_simulator_integrity.py), [`test_simulator_reproducibility.py`](../tests/test_simulator_reproducibility.py) và cấu hình.
- **Triển khai:** sinh entity, mạng quan hệ và luồng giao dịch hợp lệ theo nghiệp vụ trong `data.md`. Giữ riêng RNG/seed của thế giới, giao dịch và các thành phần sẽ bổ sung để thay đổi một kịch bản không làm đổi lịch sử nền ngoài ý muốn. Lịch nghiệp vụ công khai chỉ chứa kỳ vọng đã khai báo; không trích đáp án hậu kiểm từ truth.
- **Kiểm tra:** quan hệ entity, tính hợp lệ mốc thời gian, trạng thái/response, đơn vị tiền và quan hệ refund/reversal theo hợp đồng đã chốt. Cùng seed/config cho cùng nội dung dữ liệu khi sort theo khóa; đổi seed tạo giao dịch khác nhưng vẫn đạt bất biến.
- **Hoàn tất:** profile smoke sinh được giao dịch hợp lệ và bảng phân phối nền. Có cơ chế lưu fingerprint của nội dung canonical; không yêu cầu file DB nhị phân có hash giống nhau nếu metadata vật lý khác.
- **Nghiệm thu vòng 2 (orchestrator viết 29/10/2026):**
  - Giao dịch hợp lệ có đủ ba kênh QR/ví/thẻ theo `data.md` (Bối cảnh VN), tỷ lệ kênh và tham số trong `configs/simulator.yaml` có provenance; schema/cột mới (nếu có) định nghĩa trong `data.md` cùng thay đổi.
  - Vai trò tài khoản: cá nhân có người nhận quen, hộ kinh doanh nhận tiền qua tài khoản cá nhân (gồm hộ bán online trả nhà cung cấp), mule nội bộ chuyển tiền ra trong vài giờ (mule chỉ là năng lực của thế giới; dòng fraud thuộc P1.4). Vai trò chỉ ở private/generator metadata, không mã hóa trong ID hay cột operational.
  - Refund/reversal tách dòng, liên kết giao dịch gốc, thời gian và số tiền hợp lệ (không vượt gốc, sau gốc), append-only, có `available_at` đúng.
  - `_poisson_count` đúng cho mean rất nhỏ và lớn (hoặc config giới hạn miền mean có kiểm tra).
  - RNG/seed tách theo thành phần: thêm kênh/refund không làm đổi các luồng không liên quan ngoài ý muốn; cùng seed → cùng canonical content; đổi seed → khác nhưng giữ bất biến.
  - Validate và test bao các bất biến mới; smoke generate/validate đạt; toàn bộ pytest đạt theo quy tắc baseline smoke ở mục 7.
- **Vòng 1 đã bàn giao:** entity nền và giao dịch QR hợp lệ trước T0; lifecycle append-only; fingerprint nội dung được ghi trong private `run_metadata`. Phân phối và giả định đang dùng nằm trong config cùng provenance; các kênh, vai trò tài khoản và refund/reversal còn ở vòng 2. Vòng 2 cũng sửa `_poisson_count` (`simulator/transactions.py`) cho mean lớn/rất nhỏ hoặc giới hạn miền mean trong config — finding không chặn của review vòng 1.
- **Trạng thái bổ sung vòng 2 (29–30/10/2026):** runtime P1.3 hoàn tất: đã triển khai ba kênh, A2A và vai trò ở private metadata, refund/reversal append-only, cùng `_poisson_count` cho mean rất nhỏ/lớn. Smoke baseline đổi theo bảng bàn giao; downstream P2/P3/P4 đã chạy. Full pytest còn assertion non-pipeline `not_applicable` không đạt; kết quả và quyết định giữ assertion được ghi tại [P4 sub-plan](p4-implementation.md).
- **Cập nhật nghiệm thu 30/10/2026:** theo quyết định người dùng, chỉ case false positive minimum-support của `ep-0001` được cô lập thành strict xfail và ghi BACKLOG-5 tại [P4.2](p4-implementation.md); các assertion non-pipeline khác vẫn chạy. Full suite: 304 passed, 1 xfailed; smoke generate/validate và các kiểm tra chuẩn đều đạt. Không đổi runtime P1.3.
- **Ghi chú review fix vòng 1 (30/10/2026):** A2A thêm vào luồng trong ngày làm dịch `daily_index`, từ đó đổi `txn_id` một số dòng card-testing P1.4; P1.5 khóa RNG `reported_at` theo `txn_id`, nên 53/540 card-testing rows đổi `reported_at` và fraud calibration đổi 24 → 23. Không re-key ID ở fix này; có thể tách namespace khi triển khai P1.4. Assertion reproducibility theo world seed chỉ so luồng merchant payment và các trường độc lập với profile; không yêu cầu toàn bộ transaction/event set giống nhau vì world seed quyết định account role, familiar payee, supplier và A2A.
- **Đính chính review 30/10/2026:** câu full pytest còn assertion non-pipeline `not_applicable` không đạt ở trạng thái bổ sung phía trên đã được supersede bởi quyết định người dùng 30/10: chỉ case `ep-0001` là `xfail(strict=True)`; các case khác vẫn assert.
- **Hoàn tất fix review vòng 1 (30/10/2026):** pipeline validator đối chiếu adjustment với pre-injection amount lưu riêng trong private effect metadata và chỉ cho phép NULL ở các transaction thuộc pipeline null effect; trigger/validator giới hạn adjustment tổng, số lượng và trạng thái settlement. Default operational fingerprint giữ `6defebb0…`, không đổi bảng baseline smoke. Full suite: 311 passed, 1 xfailed; generate/validate smoke 16/16.

### P1.4 — Hành vi fraud và các ca đối chứng (`done`; vòng 2 merge 01/11/2026)

- **Mục tiêu:** hiện thực các cơ chế fraud và đối chứng nghiệp vụ đã có trong MVP mà không đưa nhãn ẩn vào feature.
- **Phụ thuộc:** P1.3.
- **File:** `simulator/fraud.py`; test integrity/incidents và báo cáo diagnostic.
- **Vòng 2:** các cơ chế, vai trò người nhận, cash-out và `legit_business_spike` theo `data.md`. Tách metadata generator khỏi thuộc tính quan sát được. Các đặc trưng phía người nhận phải suy ra được từ giao dịch đã quan sát, không ghi sẵn từ truth; định nghĩa rõ cửa sổ chưa hoàn tất.
- **Kiểm tra vòng 2:** fraud/benign đối chứng đều đi qua cùng schema và luồng ghi dữ liệu; không có cột operational mã hóa trực tiếp cơ chế hoặc nhãn. Kiểm tra quan hệ chuỗi giao dịch và cash-out; ca fraud pha trộn với benign vẫn nhất quán.
- **Hoàn tất vòng 2:** từng cơ chế sinh được ví dụ có thể kiểm tra bằng dữ liệu; báo diagnostic artefact và giới hạn simulator. Một feature phân biệt mạnh phải được giải thích từ hành vi/dữ liệu khả dụng, không tự coi AUC cao hay thấp là bằng chứng duy nhất về leakage.
- **Nghiệm thu vòng 2 (orchestrator viết 31/10/2026, chờ giao sau các task chuẩn bị P5):**
  - **Cơ chế** theo [data.md](data.md) mục 1: card testing (đã có), ATO, collusion (chỉ trước mốc drift), social engineering + cash-out qua mule nội bộ; số tiền lừa đảo sát ngưỡng 10 triệu theo config. Mỗi cơ chế có tham số và provenance trong `configs/simulator.yaml`; mechanism/role/nhóm chỉ ở private `run_metadata`.
  - **Tín hiệu quan sát được** (không ghi sẵn từ truth): social engineering lộ qua feature phía người nhận ([data.md](data.md) mục "Social engineering phải nhìn thấy được") — lần đầu chuyển cho counterparty, fan-in 7 ngày, chuyển ra trong 24 h, tài khoản mới/ngủ lâu có tiền vào; ATO cần chữ ký thiết bị/phiên. **Người dùng chốt 31/10:** thêm `device_id TEXT NULL` (mọi kênh) và `card_token TEXT NULL` (chỉ kênh `card`) vào `transactions`, biết lúc tạo; không thêm bảng thiết bị/phiên riêng ([MASTER](../MASTER_PLAN.md) mục 7). Schema đã cập nhật trong [`data.md`](data.md).
  - **Đối chứng:** `legit_business_spike` (hộ kinh doanh fan-in cao hợp lệ) và benign nhịp nhỏ đi cùng schema/luồng ghi với fraud; fan-in một mình không tách được mule khỏi hộ kinh doanh, tỉ lệ chuyển ra trong 24 h tách được — test chứng minh bằng dữ liệu sinh ra.
  - **RNG/namespace riêng** cho từng cơ chế: thêm cơ chế không đổi lịch sử nền/luồng khác ngoài ý muốn (ghi chú review P1.3 r1: tách namespace `txn_id` cho fraud); cùng seed → cùng canonical content.
  - **Kiểm tra:** chuỗi giao dịch và cash-out nhất quán (thời gian, số dư không âm nếu có mô hình số dư, quan hệ mule); không cột operational mã hóa cơ chế/nhãn; nhãn P1.5 vẫn đúng 4 trạng thái; diagnostic artefact (AUC theo feature) có giải thích. Smoke generate/validate đạt; baseline smoke đổi theo quy tắc mục 7 (cũ → mới).
  - **Luật test (AGENTS.md):** functional test qua API/CLI simulator; full suite một lần trước bàn giao.
- **Vòng 1 đã bàn giao:** `simulator/fraud.py` rải chuỗi card testing trước T0 theo `campaign_interval_days`, trên channel `card`, cùng ca benign nhiều giao dịch nhỏ qua `transactions` và `transaction_events`; victim được chọn bằng RNG fraud riêng và kết quả authorization lấy mẫu theo config. Metadata vai trò, cơ chế, ID nhóm/giao dịch và seed chỉ nằm trong private `run_metadata`. Test xác nhận schema operational không đổi, chữ ký và support theo split P2 từ dữ liệu quan sát, integrity fraud/control, tái lập và lịch sử nền không đổi khi bỏ qua generator fraud. Giới hạn vòng 1: `customer_id` đại diện cho cùng một thẻ vì schema chưa có card token; đã được xử lý trong vòng 2. Config provenance và hợp đồng hành vi nằm tại [`configs/simulator.yaml`](../configs/simulator.yaml) và [`data.md`](data.md).
- **Bàn giao vòng 2 (implementer, 01/11/2026; chờ review):** thêm `device_id`/`card_token` vào schema operational; sinh legitimate device changes, probabilistic ATO, collusion chỉ trước drift, social engineering qua mule mới khả dụng với paired cash-out, `legit_business_spike` cùng fan-in, và small benign patterns qua cùng writer. Tài khoản mule có cửa sổ lịch sử đủ bảy ngày trước lần nhận đầu, vẫn mới theo tín hiệu 30 ngày. Cơ chế P1.4 ngoài card-testing được sinh trong base và episode không mang template `clean`; nhóm `clean` giữ không có fraud P1.4 mới trong phần tương lai. Tín hiệu người nhận được suy ra as-of từ operational transaction rows. P1.5 lấy nhãn fraud từ private mechanism groups. `reports/smoke_truth.json` có AUC lớn nhất 0,590 (`amount_near_10m_vnd` ở `ep-0003`); không feature nào vượt mức giải thích mạnh 0,8, nên `strong_feature_explanations` rỗng.
  - **Smoke baseline cũ → mới:** P3 alerts: `ep-0001..0005` `[0,3,1,17,0]` → `[0,7,1,21,0]`; P3 incidents `[0,2,1,4,0]` → `[0,1,1,2,0]`; detection recall `2/2` → `2/2`; P3 matched incidents `5` → `2`, unmatched `2` → `2`. P2 conformal support: legit `136` → `149`, fraud `23` → `27`; P2 calibration cohort positive count `23` → `27`, minimum met `false` → `true`, and fallback warning removed. P4 data-ticket IDs giữ nguyên `ep-0002/incident-67c06d4b51c5196eb2894cd0` và `ep-0004/incident-367c348143656924203a5bc3`; agent-review episodes vẫn `ep-0002/3/4`. Validator summary tăng từ `16` lên `17` checks do thêm `p1_4_fraud_mechanism_chains`; Fix 1 bổ sung `operational_identifier_shape_contract` và `legitimate_new_account_control`, tổng hiện tại là `19` checks.
  - **Test:** final targeted checks đã chạy lại `13 passed`, gồm P1.4 recipient signals/chain, hai mode pipeline, P3 monitoring/runbook, P4 fake-agent/validator, P2 evaluation, deterministic fingerprint và truth isolation. Full suite chạy một lần trước các sửa baseline cuối: `413 passed, 1 xfailed, 3 failed, 8 errors`; các lỗi được truy về smoke counts/runbook và test liên quan sau đó chạy mục tiêu đạt. Không chạy full suite lần hai theo luật once; reviewer cần xác nhận trên trạng thái cuối.
  - **File:** [`simulator/`](../simulator/), [`configs/simulator.yaml`](../configs/simulator.yaml), [`model/features.py`](../model/features.py), [`tests/`](../tests/), và hai tài liệu schema/bàn giao này. Người dùng đã chốt schema; implementer không sửa MASTER và không commit.
  - **Fix 1 vòng hai — handoff implementer cuối (01/11/2026):**
    - **F1:** mọi transaction ID có dạng `txn-<64 lowercase hex>`; business/RNG draw keys được truyền tường minh tới generator và được lưu trong private label truth, không còn `_TRANSACTION_RANDOM_KEYS`. Validator phát lại reporting/delay RNG để xác nhận nhãn và ngày báo khớp trong tiến trình validate độc lập. Files: [`identifiers.py`](../simulator/identifiers.py), [`transactions.py`](../simulator/transactions.py), [`fraud.py`](../simulator/fraud.py), [`incidents.py`](../simulator/incidents.py), [`labels.py`](../simulator/labels.py), [`validate.py`](../simulator/validate.py).
    - **F2:** transaction ID và customer ID cùng dạng trên nền/fraud/control/injector; `world.legitimate_new_accounts_per_base_history=2` có provenance. Hai customer hợp lệ có giao dịch merchant trong base history sớm sau ngày bắt đầu và trước reference monitoring; không tạo thêm giao dịch episode. Cohort ID nằm trong private metadata và validator xác nhận cohort đúng số lượng, có lịch sử merchant hợp lệ, không trùng mule. Xem [`simulator.yaml`](../configs/simulator.yaml), [`transactions.py`](../simulator/transactions.py), [`data.md`](data.md) mục 1–2.
    - **F3–F7:** diagnostics giữ so sánh social engineering/control, ID-format và account-age tại [`reports.py`](../simulator/reports.py); F4 giữ assertion nghiêm ngặt sau khi bỏ dead fallback ở [`test_simulator_cli.py`](../tests/test_simulator_cli.py); F5 thay paragraph trùng bằng liên kết §1 ở [`data.md`](data.md); F6 lọc P1.4 A2A theo transaction IDs từ private mechanism metadata ở [`test_simulator_episodes.py`](../tests/test_simulator_episodes.py); F7 kiểm tra ID shape độc lập truth, clean không có mechanism group ngoài card testing, và ATO device có chữ ký chưa xuất hiện trước campaign ở [`validate.py`](../simulator/validate.py). Validator hiện có **19 checks**.
    - **Root cause — bằng chứng và lệnh:** so run baseline `/tmp/p14-fix1b-63d2d62-run` (code `63d2d62`) với replay Fix 1 trước sửa `/tmp/p14-fix1b-current-run` bằng `compute_daily_metrics(...)` rồi `compute_alerts(...)` cho `ep-0002`/`ep-0004`. Injector chọn mọi transaction trong episode sau effect start và đổi từng amount x100/NULL; nó không lấy mẫu hay RNG theo `txn_id`. Dạng ID và thứ tự ID không làm đổi effect: signatures theo event time/channel/merchant-vs-recipient/original amount bao phủ `149/149` transaction ở `ep-0002` và `158/158` ở `ep-0004`; sample ID đổi từ `txn-ep-0002-20260202-customer-000006-d0001` sang `txn-23985788c06650356cd95fca291371801e9592a76d56e0a0ac80661513fce2c7`. Fix 1 trước đó đã thêm **2** legitimate-account transactions vào từng episode, làm scope tăng `149→151` và `158→160`; tài khoản mới không có lịch sử 7 ngày nên metric observation bị censor. Base cohort có một giao dịch trong reference cũng tạo `reference_censored_samples=1`; P3 `_quality_reasons` chặn PSI có censor flag. Giá trị PSI vẫn giữ nguyên (ví dụ `ep-0002` ngày 12/02 `1.294801`, `ep-0004` ngày 12/02 `0.863071`, ngưỡng `0.25`), nhưng alerts đổi `[0,7,1,21,0]→[0,0,1,1,0]`. P3 không có trường z-score; signal so trực tiếp PSI với ngưỡng.
    - **Root fix và gate:** cohort hợp lệ được giữ trong base, đặt sớm ngoài reference và không đưa transaction vào episode. Replay cuối `/tmp/p14-fix1b-gate-run` cho scope `149/149` và `158/158` theo business signature; cùng metric PSI baseline, không cờ censoring. Lệnh `UV_CACHE_DIR=/tmp/payguard-uv-cache uv run python -m simulator generate --profile smoke --out /tmp/p14-fix1b-gate-run` validate **19/19**. Monitoring cho alerts `ep-0001..0005 = [0,7,1,21,0]`, development clean `ep-0001 = 0`, detection recall `2/2` (1.0). Test triage xác nhận data-ticket pairs cũ → mới vẫn là `{ep-0002/incident-67c06d4b51c5196eb2894cd0, ep-0004/incident-367c348143656924203a5bc3}`; incident IDs không đổi vì metric/as-of alert identity không đổi. Các pipeline records vẫn cùng hai scope; ID sample changed solely due uniform SHA ID contract.
    - **Baseline fingerprint:** canonical operational fingerprint `95bb36eca3fcd7645a04c8beb520d13e69d0714a3a01c7959ec4b6fbd452aa21` (63d2d62) → `ee508852fefb7767dcdae10467234119ba4c80b2bfaacda73f96b719d79c8a23` (Fix 1 trước root fix) → `dbbe96ea4512279b90391542e928f49d86a437b44f5bd7e9a681a97a96e8f2f8` (Fix 1 cuối). Data-ticket IDs old→new giữ nguyên như trên.
    - **Diagnostics:** ID-format AUC `0.5` cho fraud và legitimate ở mọi episode; account-age AUC của social-engineering/control là `0.20` ở `ep-0002..0005`. AUC lớn nhất là `0.589996` cho `amount_near_10m_vnd` tại `ep-0003` (base `0.585434`); đúng với dải amount synthetic đã cấu hình và không có feature vượt mức giải thích mạnh `0.8`.
    - **Tests:** targeted gate trước full suite `5 passed` (episode/cohort + CLI fresh-process validate `2`, data-ticket route `1`, direct label API `1`, fake-agent/evaluation/validator `1`). Full suite chạy đúng một lần: `421 passed, 1 xfailed, 5 failed`; hai failure là assertion summary validator cũ `17` (đã cập nhật thành `19` vì thêm hai checks), một là fixture chưa tính cohort F2, hai là fixture cho thấy cohort customer identity/profile phụ thuộc transaction seed. Đã cập nhật invariants để yêu cầu chính xác 2 control customers và dùng root config seed cho identity/profile độc lập với transaction RNG; năm test thất bại chạy riêng lại đều pass (`2` pipeline modes, `1` integrity, `2` seed reproducibility). Không chạy full suite lần hai. Final generate và validate tại `/tmp/p14-fix1-smoke` đều **19 passed, 0 failed**; `verify_plan_claims.py` kiểm `19` ví dụ, `verification_results.json` không đổi; `check_links.py`: `16` files, `0` broken links.

- **Trạng thái vòng 2:** ✅ merge 01/11/2026 ([r1](../review/findings/P1.4-r2-r1.md) CHANGES_REQUIRED: F1 định dạng `txn_id` fraud, F2 ID mule lộ role; fix 1; [r2](../review/findings/P1.4-r2-r2.md) PASS; 426 passed + 1 xfailed; smoke 19/19). Backlog không chặn: **N1** giao dịch ATO/collusion/SE/control có `created_at` giây = 0, cash-out luôn +30 phút, mule tạo 13:59 — jitter thời gian bằng RNG có namespace trước pilot (gắn với P1.6 vòng 2); **N2** cohort tài khoản mới hợp lệ chỉ 2 tài khoản × 1 giao dịch, câu chữ validator/`data.md` nói quá, thiếu check cohort trước cửa sổ reference khi tăng config; **N4** thứ tự chèn (`rowid`) trong episode tách generator khỏi nền (AUC 0,94) — tool agent không lộ vì `read_as_of` sắp theo `event_at`; ghi chú cho mọi tool mới.

### P1.5 — Label events, snapshot và label tail (`round1-done`)

- **Mục tiêu:** nhãn quan sát được xuất hiện đúng nguồn và đúng thời điểm; pending không bị chuyển thành âm trước policy.
- **Phụ thuộc:** P1.4; fixture snapshot của P1.2.
- **File:** `simulator/labels.py`; `tests/test_simulator_labels.py`, test as-of.
- **Triển khai:** sinh reporting và độ trễ từ config; snapshot theo trạng thái MVP trong `data.md`. Giữ khả năng fraud chưa bao giờ được báo trong truth riêng. Label tail không tạo thêm giao dịch cho feature. Chỉ mô phỏng feedback điều tra viên theo cơ chế được khai báo; nếu phụ thuộc alert model thực tế thì chờ P2/P3, không giả gọi model chưa có.
- **Kiểm tra:** ngay trước/đúng mốc trưởng thành; nhãn reported nhưng chưa available; fraud về sau H; fraud không được báo; nguồn nhãn phù hợp kênh; mỗi giao dịch tối đa một kết luận MVP. Quan sát nhãn dài hơn không làm thay đổi feature của thời điểm quá khứ.
- **Hoàn tất:** bảng snapshot và báo cáo độ trễ đúng trên fixture và smoke; tách số quan sát được khỏi số do oracle evaluator biết. Không triển khai sửa nhãn/disputed thuộc phần mở rộng.
- **Vòng 1 đã bàn giao:** `simulator/labels.py` sinh kết luận cho giao dịch nền P1.3/P1.4 bằng RNG riêng; delay đều rời rạc theo cấu hình; truth và trạng thái không báo chỉ nằm trong private `run_metadata`. Snapshot dùng `read_as_of`, trả bốn trạng thái H = 90; `label_delay_report` tách snapshot quan sát, oracle private và censoring. Label tail kết thúc sau giao dịch cuối ít nhất H (và đủ cận độ trễ cấu hình), không thêm giao dịch. Không mô phỏng feedback điều tra viên vì cần alert model P2/P3.

### P1.6 — Injector, calendar và truth sự cố (`done`; vòng 2 merge 01/11/2026)

- **Mục tiêu:** mỗi sự cố có tác động kiểm tra được và đáp án phục vụ evaluator, không tự quyết định alert/agent phải kết luận gì.
- **Phụ thuộc:** P1.4–P1.5.
- **File:** `simulator/incidents.py`; `tests/test_simulator_incidents.py`.
- **Triển khai:** các template và ca kết hợp theo `data.md`; ghi truth vào nguồn private. Calendar ghi kỳ vọng nghiệp vụ theo hợp đồng metric. Giữ nguyên lịch sử trước T0. Các mốc can thiệp, deadline và hành động chấp nhận được lấy từ hợp đồng nguồn, không tự tạo taxonomy khác trong code.
- **Kiểm tra:** seasonal một ngày trên nền không đều; pipeline đổi đơn vị/NULL; fraud surge/new fraud; đối chứng hợp lệ; hai nguyên nhân. So với bản đối chứng cùng seed để xác nhận phần bị tác động, giữ nguyên phần được yêu cầu không đổi. `effect_available_at` xuất phát từ bản ghi tác động thực tế, không chỉ từ timestamp ghi trong config; sửa một bản ghi sẵn có cũng phải phản ánh lúc thay đổi trở nên khả dụng.
- **Hoàn tất:** mọi template có fixture chứng minh tác động và truth đúng. Test chỉ khẳng định dữ liệu thay đổi đúng cơ chế; việc monitoring bắt được hay không thuộc P3, không ép mọi ca phải tạo alert ở P1.
- **Nghiệm thu vòng 2 (orchestrator viết 31/10/2026, sau P1.4 vòng 2):**
  - **Template còn lại** theo [data.md](data.md) mục "Kịch bản sự cố": `fraud_surge` (kiểu đã có trong train), `new_fraud` (social engineering), `legit_business_spike`, merchant mới ít giao dịch, concept drift "im lặng", seasonal biến thể "trend không có trong lịch"; mỗi template có truth đủ cột ([data.md](data.md) mục 5) với `accepted_actions` theo bảng nguồn, `rule_covered` đúng known/unknown.
  - **Hai biến thể bắt buộc cho pilot (người dùng chốt 31/10, [data.md](data.md) mục 4):** (a) hai nguyên nhân fraud + mùa vụ — hai truth thành phần cùng `parent_scenario` khác NULL, chồng thời gian có kiểm soát, lịch `business_calendar` công khai chỉ khai báo phần mùa vụ; (b) fraud một nguyên nhân rơi đúng ngày có dòng `business_calendar`. Validator chấp nhận `parent_scenario` khác NULL (hiện `simulator/validate.py` cấm) và kiểm hai thành phần cùng episode.
  - **ID trung tính cho dòng injector (audit 31/10, A1 chặn pilot):** dòng do injector chèn phải cùng namespace và định dạng ID với giao dịch nền (hiện seasonal sinh `txn-<32hex>` ở `simulator/incidents.py:345`, nền là `txn-<episode>-<ngày>-<khách>-dNNNN`, nên một câu `run_sql` tách được đúng tập dòng bị chèn); validator kiểm hình dạng ID của dòng bị chèn ⊆ hình dạng ID nền trong episode. Làm TRƯỚC khi viết injector fraud ([audit](../review/findings/AUDIT-main-31-10.md)).
  - **Độ lớn seasonal:** P3.7 hoãn seasonal-volume-match tới pilot vì smoke quá nhỏ ([bàn giao P3.7](p3-implementation.md)). P1.6 vòng 2 KHÔNG được tăng hiệu ứng seasonal để ép monitoring bắt; nếu world pilot vẫn không tách được thì báo người dùng.
  - **Kiểm tra:** so với control cùng seed cho từng template; `effect_available_at` suy từ bản ghi thật; truth chỉ ở private; template/cause không lộ qua ID/operational; ca hai nguyên nhân có đủ hai truth và matcher ghép `matched` nhiều truth theo [evaluation.md](evaluation.md) mục 2. Smoke có thể thêm template nhưng giữ 5 episode và role 1/2/2; baseline smoke đổi theo quy tắc mục 7.
  - **Luật test (AGENTS.md):** functional test qua API/CLI simulator; full suite một lần trước bàn giao.
- **Vòng 1 đã bàn giao:** [`simulator/incidents.py`](../simulator/incidents.py) triển khai `data_pipeline` nhân 100 hoặc NULL và `seasonal` tăng giao dịch hợp lệ trong một ngày, cùng business calendar công khai kỳ vọng `txn_count`; truth đủ cột chỉ ghi vào private. [`tests/test_simulator_incidents.py`](../tests/test_simulator_incidents.py) đối chiếu control cùng dữ liệu nền, kiểm tra phạm vi tác động, thời gian khả dụng thực tế và cách ly truth. Chưa triển khai template vòng 2.
- **Vòng 2 đã triển khai (bàn giao 01/11/2026):** [`simulator/incidents.py`](../simulator/incidents.py) bổ sung fraud surge, social-engineering truth, đối chứng hộ kinh doanh, merchant mới thưa giao dịch, silent concept drift, seasonal không có calendar và hai biến thể lịch bắt buộc. Hợp đồng merchant thưa giao dịch ở [data.md](data.md) chọn `truth_cause` tường minh, độc lập với số giao dịch. Không thêm phân bổ pilot hoặc template vào smoke manifest; việc khóa các episode đó vẫn thuộc P1.7.
  - **Truth và validator:** truth/effect metadata chỉ nằm trong private; effect availability lấy từ các transaction được tham chiếu. `parent_scenario` khác NULL ghép đúng cặp `fraud_surge` + `seasonal`; validator kiểm tra cùng episode, cửa sổ hiệu ứng giao nhau và đúng một calendar entry có hiệu ứng seasonal dương. Matcher trả cả hai truth với trạng thái `matched`.
  - **A1:** P1.4 đã chuyển transaction IDs nền/fraud/control sang `txn-<64 lowercase hex>`; injector P1.6 dùng cùng `simulator/identifiers.py`, và validator kiểm tra mọi transaction operational. Smoke gần nhất xác nhận 2.062 transaction cùng dạng ID.
  - **N1:** jitter phút/giây có namespace riêng cho ATO, collusion, social engineering, hộ kinh doanh, seasonal và template mới; cash-out delay thay đổi quanh cấu hình, mule creation không còn một giờ cố định. Diagnostics P1.4 có `created_at_second_zero` và thống kê fixed-delay; test xác nhận không giao dịch P1.4 được tham chiếu nào có giây 0 và cash-out không còn delay cấu hình cố định.
  - **Bằng chứng template:** [`tests/test_simulator_incidents.py`](../tests/test_simulator_incidents.py) đối chiếu cùng seed cho fraud/calendar, sparse merchant, silent drift, seasonal không calendar và hai nguyên nhân; [`tests/test_simulator_episodes.py`](../tests/test_simulator_episodes.py) xác nhận social engineering và business control không làm lộ truth qua operational data. Test triage pipeline giữ `data_ticket`.
  - **Smoke P1→P3 cũ → mới:** alerts `[0, 7, 1, 21, 0]` → `[0, 7, 1, 21, 0]`; incidents `[0, 1, 1, 2, 0]` → `[0, 1, 1, 2, 0]`; detection recall `2/2` → `2/2`. Jitter N1 đổi thời điểm, không đổi các số smoke này; clean `ep-0001` vẫn 0 alert, hai pipeline vẫn có incident để gate.
  - **Kiểm tra cuối:** `UV_CACHE_DIR=/tmp/payguard-uv-cache uv run pytest -q --durations=15` chạy một lần: **432 passed, 1 xfailed**. Generate `/tmp/p16-smoke` và validate đều **19/19 passed**; `verify_plan_claims.py` kiểm tra **19** ví dụ, file kết quả không đổi; `check_links.py` kiểm tra **16** file, **0** link hỏng. Không còn câu hỏi hợp đồng mở; P1.7 manifest/pilot allocation vẫn chưa được triển khai.
- **Vòng 2 fix 1 (implementer handoff, 01/11/2026; chờ review):** phần bàn giao vòng 2 ngay phía trên là trạng thái trước fix; các baseline và kết luận test ở đó đã được thay bằng số liệu dưới đây.
  - **F1 — vị trí và hiệu ứng:** [`simulator/episodes.py:229`](../simulator/episodes.py) chỉ giữ ATO/collusion/hoạt động hộ kinh doanh thường trong base; future episode chỉ sinh ATO tự động. [`simulator/incidents.py:786`](../simulator/incidents.py) và [`:985`](../simulator/incidents.py) gọi writer P1.4 để chèn social engineering và business spike cho injector tương ứng. [`tests/test_simulator_cli.py:244`](../tests/test_simulator_cli.py) so mule được chèn với hộ kinh doanh nền: cùng fan-in 5 và cùng older-sender share, nhưng `outflow_ratio_24h` lần lượt là 1 và 0. Test episode xác nhận hai injector thêm 10 và 5 transaction operational; không có SE/business spike dư trong base hoặc episode khác.
  - **F2 — vị trí và hiệu ứng:** [`simulator/incidents.py:1281`](../simulator/incidents.py) ghi calendar fraud bằng event name, channel và `relative_change` dương lấy từ cấu hình seasonal; chỉ chèn chín giao dịch của fraud, không sinh seasonal traffic. [`tests/test_simulator_incidents.py:702`](../tests/test_simulator_incidents.py) so ngày, tên, lag, expected effect và scope với một calendar row seasonal, đồng thời xác nhận chỉ có một fraud truth.
  - **F3 — vị trí và kiểm tra:** [`simulator/fraud.py:210`](../simulator/fraud.py) dùng chung phân phối có RNG namespace cho giờ/phút/giây bắt đầu, từng khoảng cách attempt và follow-up; cả card testing nền lẫn `fraud_surge` gọi helper này. Test functional tại [`tests/test_simulator_cli.py:112`](../tests/test_simulator_cli.py) áp lại SQL nhóm card theo khách/merchant/ngày, `HAVING COUNT(*) >= 8`. Trên smoke fix1, SQL có 6 nhóm nền (ví dụ bắt đầu `2026-02-01T03:04:07Z`, `2026-02-08T02:47:12Z`) và nhóm surge có 9 dòng bắt đầu `2026-02-12T02:13:42Z`; các giờ/phút/giây bắt đầu không còn cố định 09:00:00 như probe r1.
  - **F4 — vị trí và hợp đồng:** [`simulator/incidents.py:1029`](../simulator/incidents.py) ghi `legit_business_change.rule_covered=0`; validator tại [`simulator/validate.py:497`](../simulator/validate.py) đòi cùng giá trị. Test episode xác nhận `requires_intervention=0`, `rule_covered=0`, `accepted_actions=[]`; lý do runbook nằm tại [data.md §5](data.md#5-b%E1%BA%A3ng-truth-s%E1%BB%B1-c%E1%BB%91).
  - **N1 — overlap và phạm vi P1.7:** [`simulator/validate.py:775`](../simulator/validate.py) kiểm đúng cặp `fraud_surge` + `seasonal` cùng episode và giao cửa sổ hiệu ứng. Probe reviewer `nonoverlap.py` chạy lại và bị từ chối với `ValidationError: ... component effect windows must overlap`; test âm mới ở [`tests/test_simulator_incidents.py:681`](../tests/test_simulator_incidents.py). Câu bàn giao r2 trước đó mô tả kiểm tra overlap trước khi validator thực sự có check; fix1 mới đóng phần này. Mở rộng enum template trong manifest/private CHECK vẫn thuộc P1.7 như [data.md](data.md#private-tables), không đổi ở fix1.
  - **N2 — tài liệu pilot:** [data.md §4–§5](data.md#k%E1%BB%8Bch-b%E1%BA%A3n-s%E1%BB%B1-c%E1%BB%91) ghi pilot merchant mới dùng `no_material_change`, intervention bằng 0, actions rỗng theo MASTER mục 7; nhánh `new_fraud` vẫn có nhưng ngoài pilot.
  - **N3 — tài liệu taxonomy:** [data.md §4–§5](data.md#k%E1%BB%8Bch-b%E1%BA%A3n-s%E1%BB%B1-c%E1%BB%91) gọi rõ cặp hai nguyên nhân `fraud_surge` + `seasonal` và silent drift là cause `new_fraud`, mechanism `silent_concept_drift`.
  - **N4 — hành vi ordinary rows:** [`simulator/incidents.py:877`](../simulator/incidents.py) và [`:1591`](../simulator/incidents.py) dùng `supplier_merchants` cho silent drift/seasonal household; sparse merchant chọn customer cá nhân. Dòng injector được lấy mẫu refund/reversal theo xác suất nền trong cùng writer. [data.md §5](data.md#5-b%E1%BA%A3ng-truth-s%E1%BB%B1-c%E1%BB%91) ghi hợp đồng. Review r1 cũng ghi nhận lần đổi thứ tự rút phút/giây seasonal ở r2 làm 39/40 thời điểm seasonal smoke đổi so với main, trong khi số dòng và `relative_change` giữ nguyên.
  - **Fingerprint:** smoke fix1b giữ `29a16830bcafddc49fd3572e983152f9f482f3dc8f49a00cb5a02bb54f6271ca` (r2 trước fix1: `699609f0160e…`). Bảng dưới ghi các thay đổi baseline đã duyệt; P3 monitoring code/config không đổi.

#### P1.6 fix-1 — baseline cuối sau fix1b

| Đo số / assertion | Trước fix1b | Fix1b | Nguyên nhân |
|---|---:|---:|---|
| Calibration fraud positives | 27 | 19 | SE bị rút khỏi base và các episode không thuộc template `new_fraud`. |
| Calibration legitimate sample count | 149 | 142 | Cùng thay đổi population calibration do F1; các giá trị được assert riêng theo lớp. |
| `calibration_minimum_met` / warning | `true` / `[]` | `false` / `isotonic_support_below_25_per_class; fallback_method=sigmoid` | 19 positives thấp hơn ngưỡng khóa 25; trước P1.4, 23 positives cũng cho `false` và cùng fallback warning. `support_status` vẫn `sufficient`; fallback vẫn được dùng. |
| P3 alerts `[ep-0001..ep-0005]` | `[0,7,1,21,0]` | `[0,3,1,21,0]` | Pipeline effect scope nhỏ hơn sau khi bỏ 10 SE + 5 business-spike transaction trong `ep-0002`; clean vẫn 0. |
| P3 incidents `[ep-0001..ep-0005]` | `[0,1,1,2,0]` | `[0,2,1,2,0]` | Scope transaction thay đổi làm `ep-0002` tách thành hai incident; monitoring không đổi. |
| Dòng thuộc pipeline `affected_transaction_ids` | `ep-0002: 149`, `ep-0004: 158` | `ep-0002: 134`, `ep-0004: 143` | Mỗi pipeline episode giảm đúng 15 dòng: 10 SE + 5 business spike; đây là scope của `data_pipeline` truth, không phải thay đổi monitoring. |
| Incident matched / detection recall | `2` / `2 of 2` | `3` / `2 of 2` | Incident amount phụ của `ep-0002` cũng match cùng truth; truth detection vẫn đếm hai pipeline episode, recall không giảm. |

Runbook amount incident IDs và outcome fix1b (test giữ `data_ticket` equality với `_EXPECTED_SMOKE_DATA_TICKETS`):

| Episode | Amount incident ID | Action | `evidence_status` | Lý do |
|---|---|---|---|---|
| `ep-0002` | `incident-67c06d4b51c5196eb2894cd0` | `data_ticket` | `sufficient` | First-jump; checklist `unit_or_null_change` pass. |
| `ep-0002` | `incident-e2e5c62596ba75eca194ffad` | `human_review` | `insufficient` | Incident phụ; checklist `not_applicable`, `data_ticket` yêu cầu checklist pass. |
| `ep-0004` | `incident-367c348143656924203a5bc3` | `data_ticket` | `sufficient` | First-jump; checklist `unit_or_null_change` pass. |

Smoke manifest vẫn chỉ dùng `clean/data_pipeline/seasonal` cho đến P1.7 nên trước injector không có customer episode-local; test manifest giữ assertion này. Test P1.6 gọi `inject_new_fraud` và xác nhận mule customer thuộc episode `ep-0002`, không thuộc base.

- **Kiểm tra fix1b:** 5 test từng fail/error và 9 test P1.6 mục tiêu chạy riêng, **14 passed**. Bài metrics từng báo error đã được chạy đầy đủ; nguyên nhân là các baseline alert/matched cũ, không phải fixture bị bỏ qua. Smoke `/tmp/p16-fix1-smoke` generate + validate **19/19**; roles `1/2/2`, clean `ep-0001` 0 alert, first-jump của hai pipeline là `data_ticket`, recall `2/2`. `verify_plan_claims.py` xác minh 19 ví dụ và `review/verification_results.json` không đổi; `check_links.py` kiểm 16 file, 0 link hỏng. Full suite **không chạy lại theo chỉ đạo orchestrator**; lượt trước trên baseline chưa cập nhật là 429 passed, 1 xfailed, 4 failed, 1 error; orchestrator sẽ chạy suite trên cây cuối.

- **Vòng 2 fix 2 (implementer handoff, 01/11/2026):**
  - **F5 — seed và khóa nhãn:** [`simulator/incidents.py`](../simulator/incidents.py) yêu cầu `inject_new_fraud` nhận `seed` cùng `transaction_random_keys` và truyền cả hai vào writer P1.4; `inject_legit_business_spike` cũng yêu cầu seed tường minh. Đã bỏ `_episode_seed`, nên injector không đọc manifest được ghi sau injection và label generation. Test functional tại [`tests/test_simulator_episodes.py`](../tests/test_simulator_episodes.py) chèn SE trước bước sinh nhãn: đủ 10/10 private fraud outcomes, toàn bộ dùng khóa RNG, và 7 nhãn được báo khớp với operational `label_events`; một smoke chuẩn riêng validate thành công. Reviewer `wire.py` đã chạy với seed/keys: label generation qua được (10 SE rows, 7 reported labels); CLI probe trả `rc=1` chỉ vì probe cố thêm `new_fraud` truth vào `ep-0002` đang khóa `data_pipeline`. Hợp đồng template giữ nguyên cho P1.7.
  - **N5 — tài liệu:** [data.md §1](data.md#1-simulator-n%E1%BB%81n) nói rõ base gồm ATO, collusion và hoạt động hộ kinh doanh thường; episode tự sinh ATO; social engineering và `legit_business_spike` chỉ do injector/template tương ứng. Link F2 tới test incidents đã trỏ đúng thư mục `tests/`.
  - **N6 — cửa sổ event:** [`simulator/incidents.py`](../simulator/incidents.py) chặn refund/reversal tại `T0 + duration_days`; validator có check riêng `transaction_event_episode_window` cho `event_at` và `available_at` của mọi transaction event trong episode. Test âm từ chối event đặt đúng tại thời điểm kết thúc. Probe `adjend.py` thêm 300 silent-drift transactions ở ngày cuối: refund duy nhất xảy ra lúc `2026-03-02T10:42:05Z`, không có event muộn; cả 2.035 event thỏa cửa sổ. Probe vẫn fail truth-template check vì cố gắn truth vào episode `clean`, còn check cửa sổ event PASS. Schema được mô tả tại [data.md §2](data.md#2-schema-và-hợp-đồng-as-of).
  - **Baseline và gate:** nội dung smoke không đổi so với fix1b; canonical operational fingerprint vẫn `29a16830bcafddc49fd3572e983152f9f482f3dc8f49a00cb5a02bb54f6271ca`. Smoke có 5 episode, roles 1/2/2, `ep-0001` 0 alert; hai pipeline giữ first-jump `data_ticket`, detection recall 2/2. Validator tăng từ 19 lên **20 checks** do check episode-event window mới.
  - **Kiểm tra cuối:** full suite chạy đúng một lần: **436 passed, 1 xfailed** trong 783.32 giây. `generate` và `validate` tại `/tmp/p16-fix2-smoke` đều **20/20 passed**. `verify_plan_claims.py` xác minh 19 ví dụ; hash `review/verification_results.json` giữ `858baaf241cffb1ecbae8ae25ddd748b6ce66ac8aa2e9e156db87f091f41bd16`. `check_links.py`: 16 file, 0 link hỏng. Không commit; không triển khai N7 hay manifest/template wiring P1.7.

- **Trạng thái vòng 2:** ✅ merge 01/11/2026 ([r1](../review/findings/P1.6-r2-r1.md) CHANGES_REQUIRED, fix 1 theo quyết định người dùng F1/F2; [r2](../review/findings/P1.6-r2-r2.md) CHANGES_REQUIRED F5; fix 2 ngắn người dùng duyệt, merge không review lại; 436 passed + 1 xfailed; smoke 20/20). Chuyển P1.7: nối template vào generate + enum manifest/CHECK/validator; N7 tài khoản mới hợp lệ trong episode. Ghi chú r2 không chặn: hộ kinh doanh trong dòng injector dùng supplier 100 % (nền ~84 %); nhánh fallback overlap hai nguyên nhân có thể đặt giờ ngoài phân phối card testing.

### P1.7 — Episode và manifest (`round1-done`)

- **Mục tiêu:** sinh được bộ episode dùng chung cho mọi baseline, khóa vai trò trước tuning.
- **Phụ thuộc:** P1.6.
- **File:** `simulator/episodes.py`, `configs/episodes.yaml`; `tests/test_simulator_episodes.py`, test reproducibility.
- **Triển khai:** cùng thế giới/lịch sử trước T0, các tương lai riêng theo seed. Cách xuất/đọc lịch sử chung phải tuân theo biểu diễn đã chốt ở P1.1–P1.2; không cần mỗi episode có một bản vật lý riêng nếu snapshot vẫn cách ly đúng. Sinh manifest với vai trò theo `evaluation.md`; bản tối thiểu theo MASTER, cỡ benchmark cuối khóa sau pilot chi phí. Nhãn lịch mặc định của dòng demo không tự lọt vào episode danh nghĩa.
- **Kiểm tra:** thêm/bỏ/reorder episode không đổi dữ liệu của episode có cùng seed/config; role và seed tái lập được; các tập tách ở cấp episode; episode hai nguyên nhân vẫn là một đơn vị chia tập; không có giao dịch từ episode khác trong truy vấn.
- **Hoàn tất:** manifest có fingerprint và cấu hình đủ để tái tạo. P1 không dùng test để chỉnh tham số cho P2/P3/P4; xem lại test để tune khiến seed đó không còn held-out và phải sinh seed mới theo protocol.
- **Vòng 1 đã bàn giao:** [`simulator/episodes.py`](../simulator/episodes.py) sinh một lần lịch sử base, rồi 5 episode smoke có role 1/2/2, seed riêng dẫn xuất từ root seed + `episode_id`, giao dịch 30 ngày, card testing P1.4, label P1.5 và tối đa một template P1.6. Manifest có role/seed/template, fingerprint nội dung canonical và config hash; manifest/truth/template ở private, operational `episodes` chỉ có ID + role. Smoke có clean, `data_pipeline` và `seasonal`; chưa sinh pilot 60.
- **Nghiệm thu vòng 2 (orchestrator viết 01/11/2026, sau P1.6 vòng 2):**
  - **Profile `pilot`** trong `configs/episodes.yaml`: 60 episode đúng bảng phân bổ [data.md](data.md) mục 4 (role 12/24/24, từng kịch bản × role đúng số). Gán template cho episode tất định và khóa trong manifest trước mọi tuning; ca hai nguyên nhân là một đơn vị chia tập. Template ngoài bảng (seasonal "trend không có trong lịch") không gán trong pilot; muốn gán → hỏi người dùng.
  - **Seed (audit B4):** smoke giữ nguyên `p1.7.episode.v1(root_seed, episode_id)` để bảo toàn seed/baseline đã khóa; pilot dùng `p1.7.episode.v2(root_seed, profile, episode_id)`. Validator kiểm seed calibration/test pilot không giao với manifest smoke. Thêm/bỏ/reorder episode không đổi dữ liệu của episode cùng seed/config.
  - **Nối template P1.6 vào generate:** mọi template trong bảng chạy qua pipeline với seed và khóa RNG tường minh; mở rộng enum template trong manifest, CHECK private và hợp đồng template của validator ([data.md](data.md) mục private tables) trong cùng thay đổi. Merchant mới dùng `no_material_change` (`requires_intervention = 0`, `accepted_actions` rỗng — quyết định 01/11, MASTER mục 7). Dòng lịch của biến thể fraud trùng ngày lịch lấy tham số từ config seasonal, không literal. Validator khóa injector theo template manifest (SE chỉ ở `new_fraud`, spike chỉ ở `legit_business_spike`).
  - **Đã bị thay thế bởi quyết định N7 ngày 01/11/2026:** người dùng trước đó chốt mọi episode không clean có thêm vài tài khoản mới hợp lệ trong cửa sổ tương lai, đủ lịch sử để không censor metric monitoring; tham số kèm provenance. Kiểm tra trước đó dùng tập customer tạo sau T0.
  - **Quyết định N7 ngày 01/11/2026 (người dùng chốt; review P1.7 r1 N5):** áp dụng controls cho mọi episode, kể cả clean. Xem hợp đồng và lịch tạo tại [data.md §4](data.md#4-episode--th%E1%BA%BF-gi%E1%BB%9Bi-song-song).
  - **Sinh pilot:** `generate --profile pilot` + `validate` đạt; ghi thời gian chạy và fingerprint. Chỉ đọc kết quả integrity của validator; KHÔNG xem metric/alert/score của episode calibration/test để chỉnh tham số (vi phạm → seed đó mất tư cách held-out, phải sinh seed mới). Báo cáo support đầy đủ thuộc P1.8.
  - **Smoke:** giữ 5 episode, role 1/2/2; gate: hai pipeline first-jump → `data_ticket`, clean development 0 alert, recall không giảm; baseline cũ → mới theo mục 7.
  - **Luật test (AGENTS.md):** functional test qua API/CLI simulator; full suite một lần trước bàn giao.

- **Bàn giao implementer P1.7 vòng 2 (01/11/2026, tiếp tục p17b):** Hoàn tất profile pilot, manifest và validation. Runtime/config files: `configs/episodes.yaml`, `simulator/config.py`, `simulator/episodes.py`, `simulator/schema.py`, `simulator/__main__.py`, `simulator/validate.py`; contracts tại [data.md §4](data.md#4-episode--th%E1%BA%BF-gi%E1%BB%9Bi-song-song) và allocation arithmetic tại `review/verify_plan_claims.py`. Tests/docs: `tests/test_simulator_episodes.py`, `tests/test_simulator_cli.py`, `tests/test_simulator_incidents.py`, `docs/data.md`, `docs/p1-implementation.md`. Smoke giữ seed v1; pilot dùng seed v2 kèm profile. N7 controls có private metadata và mỗi account đủ ≥7 ngày trước transaction.

  **Allocation đọc từ private `episode_manifest` của `/tmp/p17-pilot`:**

  | Template | Dev | Cal | Test | Tổng |
  |---|---:|---:|---:|---:|
  | clean | 4 | 8 | 8 | 20 |
  | two_cause_fraud_seasonal | 2 | 4 | 4 | 10 |
  | fraud_calendar | 1 | 2 | 2 | 5 |
  | seasonal | 1 | 2 | 2 | 5 |
  | data_pipeline | 1 | 2 | 2 | 5 |
  | fraud_surge | 1 | 2 | 2 | 5 |
  | new_fraud | 1 | 1 | 1 | 3 |
  | legit_business_spike | 1 | 1 | 1 | 3 |
  | new_merchant_sparse_history | 0 | 1 | 1 | 2 |
  | silent_concept_drift | 0 | 1 | 1 | 2 |
  | **Tổng** | **12** | **24** | **24** | **60** |

  **B4/N7:** pilot validator báo 48 seed calibration/test disjoint toàn bộ 5 seed smoke. N7 kiểm 40 episode non-clean, 240 privately legitimate control transactions ≥7 ngày tuổi; SQL customer-created-after-T0 trả 270 transaction so với 50 transaction trong `new_fraud` truth, hai tập khác nhau. Không đọc metric/alert/score pilot.

  **Smoke baseline:** N7-off isolation (runtime hook chỉ ở `/tmp`) khớp fingerprint legacy `29a16830bcafddc49fd3572e983152f9f482f3dc8f49a00cb5a02bb54f6271ca`; N7-on là `c2db2990c54c53eb17f1afa27f511d1a5b34e3aa534ebb590fe33d018484482d`. Cả hai có alert `[0,3,1,21,0]`, incidents `[0,2,1,2,0]`, recall `2/2`, và đúng hai pipeline `data_ticket`: `(ep-0002, incident-67c06d4b51c5196eb2894cd0)`, `(ep-0004, incident-367c348143656924203a5bc3)`. N7-on smoke có 24 control transaction; gate không đổi.

  **Pilot:** `generate --profile pilot` hoàn tất trong 2516.68 giây, fingerprint `fb1c529493cc5a075da571c6c00fcefc2e33f86193c18605994193efc9352b63`; generate validator và lệnh validate riêng đều 20/20. Smoke `/tmp/p17-smoke` generate/validate đều 21/21, fingerprint N7-on như trên.

  **Test/kiểm tra cuối:** full suite duy nhất đã chạy một lần: ban đầu 431 passed, 1 xfailed, 5 failed; hai lỗi là expected check-count cũ (20 thay vì 21), ba lỗi do template validator áp vào fixture không có manifest hoặc fixture khai báo seasonal thay vì two-cause. Sửa theo đúng hợp đồng, không nới gate; chạy lại 5 targeted instances, cả 5 passed. Không chạy full suite lần hai theo giới hạn once-only. `verify_plan_claims.py`: 20 ví dụ; `review/verification_results.json` được khôi phục về SHA-256 `858baaf241cffb1ecbae8ae25ddd748b6ce66ac8aa2e9e156db87f091f41bd16`. `check_links.py`: 16 files, 0 broken. Không commit; MASTER_PLAN vẫn do orchestrator cập nhật sau merge. Open item: full suite chưa chạy lại sau các targeted fixes.

  **Fix 1 — bàn giao implementer P1.7 vòng 2 (01/11/2026, sau review r1 CHANGES_REQUIRED):**

  - **F1/N7:** controls được tạo sau `_inject_template`, giữ nguyên đầu ra injector P1.6. Hai customer hợp lệ được thêm cho mọi episode, gồm clean; với `new_fraud`, customer đầu có cùng timestamp tạo với mule trước intervention, customer thứ hai có ngày tạo seeded trong ngày 9–12 sau T0 và trước deadline. Giao dịch đầu bắt đầu ngày 27, bảo đảm ≥7 ngày lịch sử. Validator dùng đúng `episode_id`, `event_at > T0`, `available_at <= deadline_at`; tập customer hiện thấy là strict superset mule và không có phép so sánh timestamp đơn lẻ nào trích đúng tập mule. Tham số/provenance ở [episodes.yaml](../configs/episodes.yaml), hợp đồng tại [data.md §4](data.md#4-episode--th%E1%BA%BF-gi%E1%BB%9Bi-song-song).
  - **F2:** `n7_controls` gồm toàn bộ config/provenance trong từng episode `config_json`; functional test xác nhận fingerprint đổi khi đổi N7 jitter. N1 sửa ví dụ allocation thứ 20 để đọc `configs/episodes.yaml`; `verify_plan_claims.py` ghi đủ ma trận scenario × role và `review/verification_results.json` có SHA-256 `f84a0266f6894abf6f2279272d6b4858926ce09efe135563c5186cb1c7d19da9`.
  - **N2/N4:** validator khóa số lượng mechanism group theo template và chỉ cho `sparse_new_merchant` thuộc template `new_merchant_sparse_history`; negative test gắn group này với `new_fraud` bị từ chối. Tests chức năng kiểm ma trận pilot, seed v2 và từ chối cal/test overlap với smoke, N7 cùng-timestamp rejection, và thêm/bỏ pilot template trên subprofile có `new_fraud`; episode giữ nguyên content/config fingerprint khi thêm episode sau nó.
  - **N3 — giới hạn đã biết, không redesign:** customer IDs là hash rút gọn sáu chữ số; allocator giữ tập `occupied` xuyên các episode. Va chạm hiếm được giải quyết deterministic theo thứ tự sinh nên thêm episode trước một ID có thể đổi ID N7 về sau nếu xảy ra va chạm. Review yêu cầu ghi nhận giới hạn, chưa thay đổi chiến lược ID.
  - **N6 — hành vi cuối:** vì N7 được chèn sau template injector, control transactions của `data_pipeline` không nằm trong danh sách bị nhân ×100/đặt NULL. Cách chèn này giữ nguyên tập affected transaction và output P1.6.
  - **Smoke gate và fingerprint:** N7-on trước fix `c2db2990c54c53eb17f1afa27f511d1a5b34e3aa534ebb590fe33d018484482d` → hiện tại `3706257ffe8c825417fce693ec369405ede903f1456fffa261821cfc353d82ea`. P3 alert counts theo `ep-0001..0005` giữ `[0,3,1,21,0]`; incidents `[0,2,1,2,0]`; clean development `ep-0001` là 0 alert; detection recall giữ `2/2` (1.0). P4 vẫn tạo đúng hai `data_ticket`: `ep-0002/incident-67c06d4b51c5196eb2894cd0` và `ep-0004/incident-367c348143656924203a5bc3`.
  - **Kiểm tra cuối:** smoke `/tmp/p17-fix1-smoke` generate và validate **21/21**, fingerprint như trên. `verify_plan_claims.py` xác minh 20 ví dụ; `check_links.py` cuối kiểm tra 16 files, 0 broken. Full suite chạy một lần: **441 passed, 1 xfailed**, 829.26 giây. Pilot `/tmp/p17-pilot-fix1` generate validator **20/20** và validate riêng **20/20**; fingerprint `6c31982751e9e9087b9fb03c5549ce003a8feca5509849656c2fff2f12a512a2`, runtime generate `1908.043114` giây. Integrity summary: 60 manifest assignments (12/24/24), 48 cal/test seeds disjoint với 5 smoke seeds, 50 incident truth rows, 360 control transactions trên cả 60 episode, ba `new_fraud` as-of checks strict superset. Không đọc metric/alert/score calibration/test. Không commit; không sửa MASTER_PLAN.

  **Fix 2 — bàn giao implementer P1.7 vòng 2 (01/11/2026, người dùng duyệt sau review r2; không review lại):**

  - **F1′ — transaction và episode side channel:** validator kiểm strict superset ở mức transaction theo đúng `episode_id` và `available_at <= deadline_at`; thêm check structural để mọi episode có cùng 3 customer sau T0, không có creation timestamp trùng, cùng first-transaction offsets `[8, 15, 27]`. Negative tests xác nhận truth-only transaction set và shape Fix 1 (`new_fraud` 3/1 so với episode khác 2/0) đều bị từ chối. Contract và lịch hiện tại ở [data.md §4](data.md#4-episode--th%E1%BA%BF-gi%E1%BB%9Bi-song-song).
  - **N7/N8 — lịch và phạm vi injector:** `creation_offset_days` được áp dụng cho mọi template; không sao chép timestamp mule. Tài khoản đối chứng bắt đầu hoạt động trước deadline ngày 15. Customer và giao dịch N7 được thêm sau template injector nên pipeline truth chỉ gồm giao dịch mà P1.6 thực sự tác động; amount của control được lấy mẫu từ miền cấu hình. Hành vi chi tiết/provenance tại [episodes.yaml](../configs/episodes.yaml).
  - **N9:** `review/verify_plan_claims.py` dùng stdlib, không import PyYAML; `python3 review/verify_plan_claims.py` kiểm 20 ví dụ.
  - **N10 — ghi chú merchant:** merchant entity trong episode chỉ xuất hiện ở `new_merchant_sparse_history`; đây là event của template đã có từ P1.6.
  - **Smoke gate mới:** fingerprint `3706257ffe8c825417fce693ec369405ede903f1456fffa261821cfc353d82ea` → `0bf373c506ed5d9c906513b3984aa07146f148d600c6fe245de40f1ee39923e0`. Alerts theo `ep-0001..0005`: `[0,3,1,21,0]` → `[0,3,1,22,0]`; incidents `[0,2,1,2,0]` → `[0,2,1,2,0]`. Clean development giữ 0 alert; detection recall `2/2` (1.0). Hai pipeline first-jump tiếp tục thành `data_ticket` từ rule, cùng IDs `ep-0002/incident-67c06d4b51c5196eb2894cd0` và `ep-0004/incident-367c348143656924203a5bc3`. `ep-0004` smoke có thêm một alert P3 so với baseline; test hiện có kỳ vọng cũ 21 và không bị sửa.
  - **Smoke integrity:** `/tmp/p17-fix2-smoke-gate` generate + validate **21/21**; lệnh AGENTS trên `data/p1-smoke` cũng generate + validate **21/21**, fingerprint trùng smoke gate.
  - **Pilot integrity:** `/tmp/p17-pilot-fix2` được generate đúng một lần, runtime `2105.09713` giây, fingerprint `bd46321748bd31b8b37e7f4713351c3e20c581e704f1d98b28c414d5c9a443c6`. Embedded validator ban đầu phát hiện thiếu `incident_truth_id` trong SELECT N7; đã sửa validator và validate lại chính artifact này **20/20**. Integrity: 60 assignments (12/24/24), 48 cal/test seeds disjoint với 5 smoke seeds, 50 incident truth rows, 297 control transactions, 3 `new_fraud` as-of checks strict superset, cùng customer/timestamp/timing shape trên 60 episode. Không đọc metric/alert/score pilot.
  - **Tests:** full suite chỉ chạy một lần, trước các chỉnh sửa N7 cuối: **441 passed, 1 xfailed, 1 failed, 1 error** trong 886.50 giây. Sau chỉnh sửa, targeted tests pass gồm N7 transaction negative, episode-shape negative, `new_fraud` label/validation, pilot template/fingerprint invariance, và runbook pipeline routing. Smoke metrics target vẫn báo lỗi duy nhất vì assertion cũ đòi ep-0004 = 21, trong khi kết quả hiện tại là 22; không sửa test và không chạy full suite lần hai theo giới hạn once-only. `verify_plan_claims.py` và `check_links.py` được chạy sau handoff. Không commit; không sửa MASTER_PLAN.

### P1.8 — CLI, validation và bàn giao P2/P3 (`round1-done`; vòng 2 chưa làm)

- **Mục tiêu:** sinh và kiểm tra dữ liệu từ CLI, trả mã lỗi khi vi phạm hợp đồng; bàn giao đủ thông tin để P2/P3 dùng được.
- **Phụ thuộc:** P1.1–P1.7.
- **File:** [`simulator/__main__.py`](../simulator/__main__.py), [`simulator/validate.py`](../simulator/validate.py), [`simulator/reports.py`](../simulator/reports.py), test [`tests/test_simulator_cli.py`](../tests/test_simulator_cli.py); báo cáo trong artifact run.
- **Triển khai:** CLI generate/validate; lưu config đã resolve, seed, phiên bản schema/code, fingerprint, thời gian chạy và kết quả kiểm tra. Tạo báo cáo support cho các cohort của P2, phân phối theo kênh/thời gian, nhãn trễ và độ đầy đủ cửa sổ. Báo cáo có dùng truth là private, không cấp cho tools.
- **Kiểm tra:** chạy toàn bộ smoke tests, sinh lại cùng config để đối chiếu canonical data; chạy pilot scale và kiểm tra support của các tập P2. Khi thiếu positive/reference, dừng bàn giao tập đó và ghi rõ nguyên nhân; không tự đổi split hoặc dùng oracle label để làm kết quả đẹp.
- **Hoàn tất:** CLI và các test runtime thực sự chạy đạt; P2/P3 nhận được artifact đúng contract và các giới hạn đã ghi. Cập nhật trạng thái MASTER và lệnh kiểm tra module trong AGENTS khi runtime tồn tại.
- **Vòng 1 đã bàn giao:** `generate --profile smoke --out ...` tạo SQLite operational/private tách riêng; `validate --run ...` kiểm tra schema, khóa/FK, as-of/episode, truth boundary, ID, pending và fingerprint, trả mã lỗi khi sai. Fingerprint này cố định dữ liệu do generator tạo; các dòng dẫn xuất về sau trong `model_scores`, `daily_metrics`, `alerts`, `incidents` không làm đổi fingerprint ban đầu. Báo cáo operational ghi support theo split đã khóa, kênh/thời gian, nhãn trễ và độ đầy đủ cửa sổ; oracle/support dùng truth ở `private/reports/smoke_truth.json`. Thiếu positive/negative bắt buộc hoặc reference sẽ chặn bàn giao; smoke calibration có 24 positive vận hành, dưới ngưỡng isotonic 25 nên ghi cảnh báo và dùng fallback sigmoid. Không đổi split hay dùng truth để bù. Pilot 60 và báo cáo support vòng 2 còn hoãn sau ★.
- **Backlog vòng 2 (QA/review vòng 3, không chặn):** validate tin danh sách `txn_id` bị tác động do injector ghi trong `run_metadata` private — sửa đồng thời truth và danh sách đó (chọn giao dịch khác trong episode/cửa sổ future) chưa bị phát hiện; cần kiểm tra độc lập rằng các giao dịch đó mang hiệu ứng sự cố. Key `p1.6.incident_effect.v1` mang tiền tố P1.6 dù thêm ở P1.8.

## 5. Lệnh mục tiêu và cách chạy theo từng mốc

Lệnh smoke thực tế cho vòng 1:

```bash
uv run python -m simulator generate --profile smoke --out data/p1-smoke
uv run python -m simulator validate --run data/p1-smoke
uv run pytest
```

- Mốc đầu: P1.1–P1.2, config + fixture nhỏ và kiểm tra as-of, chưa cần volume lớn.
- Mốc tiếp: P1.3–P1.6, kiểm tra từng cơ chế trên smoke trước khi ghép thành benchmark.
- Mốc bàn giao vòng 1: P1.7–P1.8, smoke manifest và export có kiểm định/báo cáo operational-private.
- Sau ★, vòng 2 mới chạy pilot 60 và báo cáo support đầy đủ.
- Sau thay đổi tài liệu/protocol: chạy hai lệnh hiện có trong [AGENTS.md](../AGENTS.md). Các ví dụ `review/` không thay thế test runtime P1.

## 6. Checklist nghiệm thu P1

- [ ] Config/provenance đủ, giá trị giả định được ghi rõ; schema chi tiết ở đúng nguồn.
- [ ] Fixture và smoke đạt integrity, as-of/episode isolation, snapshot nhãn và reproducibility.
- [ ] Operational export không có truth hoặc metadata đáp án; giới hạn kiểm tra truy cập P1/P4 được ghi rõ.
- [ ] Các cơ chế có đối chứng và fixture kiểm tra tác động; truth khớp thời điểm dữ liệu thực sự khả dụng.
- [ ] Label tail, reporting và các cửa sổ chưa đủ quan sát được xử lý đúng.
- [ ] Bộ episode/manifest đúng protocol, khóa vai trò và tái tạo được bằng seed/config.
- [ ] Báo cáo support các tập P2, tính đầy đủ dữ liệu và artefact diagnostic đã được review; thiếu support được báo thay vì âm thầm đổi protocol.
- [ ] Lệnh generate/validate và test runtime chạy được; mã lỗi không che vi phạm hợp đồng.
- [ ] MASTER cập nhật trạng thái bằng kết quả đã chạy; AGENTS có lệnh test module thực tế.

## 7. Task tiếp theo

**Sau ★ — P1 vòng 2.** Mở rộng P1.3, P1.4, P1.6 và P1.7 theo bảng hai vòng; P1.8 chạy pilot 60 và hoàn thiện báo cáo support. P1.8 vòng 1 smoke đã bàn giao.

**Thứ tự P1 vòng 2 — người dùng duyệt 29/10/2026.** Bốn đợt nối tiếp; mỗi đợt review Opus và merge trước khi mở đợt sau:
1. P1.3 vòng 2 (kênh, vai trò tài khoản, refund/reversal, sửa `_poisson_count`).
2. P1.4 vòng 2 (đủ cơ chế fraud, social engineering, cash-out, `legit_business_spike`).
3. P1.6 vòng 2 (các template còn lại trong [data.md](data.md) mục Kịch bản sự cố, ca hai nguyên nhân). Phải sinh được hai nguyên nhân fraud + mùa vụ và fraud một nguyên nhân trùng ngày `business_calendar` theo cơ cấu pilot chốt 31/10 ([data.md](data.md) mục 4).
4. P1.7 + P1.8 vòng 2 (pilot 60 episode 12/24/24, báo cáo support đầy đủ, ca P4.6 thứ ba). Seed episode pilot phải khác seed smoke (audit B4: hiện chỉ dẫn xuất từ `(root_seed, episode_id)` nên pilot ep-0002…0005 trùng seed smoke đã bị soi); validator kiểm seed calibration/test không giao seed smoke. Manifest stratify theo bảng phân bổ 60 episode [data.md](data.md) mục 4 (chốt 31/10); sửa `configs/episodes.yaml` (`multi_cause_episode_count`), `simulator/config.py` (đang cứng 4) và validator cùng lúc; thêm ví dụ số học của bảng vào `review/verify_plan_claims.py` (đổi protocol).

**Backlog P1.3 vòng 2 (không chặn, [findings r2](../review/findings/P1.3-r2-r2.md)):** test trigger refund chèn adjustment hợp lệ trước nên che các ca null/vượt gốc/không sau gốc (mutation vẫn pass) → thử từng ca trên giao dịch chưa có adjustment; tập data_ticket tường minh thiếu kiểm `source='rule'`, `cause='data_pipeline'` và tổng số dòng; thiếu test âm cho các nhánh validator pre-injection (`validate.py:843/756/762/547/686`); `data.md` mục effect reference chưa link mô tả `pre_injection_amount_by_txn_id`.

**Quy tắc baseline smoke (người dùng duyệt 29/10/2026):** khi thế giới nền đổi làm đổi dữ liệu smoke, coder được cập nhật các số đếm/fingerprint smoke ghi cứng trong test P1–P4 (ví dụ số `model_scores`, số incident, số alert). KHÔNG được nới bất biến (AS-OF, truth cô lập, pending không phải nhãn âm, quy tắc gate, khóa manifest) hay xóa assertion. Mỗi số đổi phải liệt kê cũ → mới kèm lý do trong báo cáo bàn giao; reviewer đối chiếu từng số.
