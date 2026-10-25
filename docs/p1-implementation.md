# P1 — Sub-plan triển khai simulator và incident injector

> Trạng thái: **đã lập kế hoạch, chưa triển khai runtime**. Phạm vi và mốc P1: [MASTER_PLAN.md](../MASTER_PLAN.md).
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
| P1.2 Schema, storage, as-of | Đầy đủ | — |
| P1.3 Thế giới, giao dịch hợp lệ | Tối thiểu: entity + giao dịch hợp lệ có nhịp ngày, profile smoke | Đủ kênh, vai trò tài khoản, refund/reversal |
| P1.4 Fraud | **Một** cơ chế (card testing) | Đủ cơ chế, social engineering, cash-out, `legit_business_spike` |
| P1.5 Nhãn | Đầy đủ | — |
| P1.6 Injector, calendar, truth | 1–2 template: `data_pipeline` (đổi đơn vị) + `seasonal` một ngày; truth đủ cột | Các template còn lại, ca hai nguyên nhân |
| P1.7 Episode, manifest | Smoke: vài episode, manifest có vai trò | Pilot 60 episode |
| P1.8 CLI, validation | `generate`/`validate` cho smoke | Pilot, báo cáo support đầy đủ |

Vòng 1 xong khi P2 và P3 tối thiểu đọc được artifact smoke; trạng thái task ghi `round1-done` trước khi lên `done`.


### P1.1 — Cấu hình và provenance (`todo`)

- **Mục tiêu:** mọi tham số được sử dụng có giá trị, đơn vị, lý do và nguồn hoặc nhãn giả định rõ ràng.
- **Phụ thuộc / đầu vào:** các nguồn đã chọn trong `data.md` / [rationale.md](rationale.md); phạm vi MVP và các vai trò episode hiện hành.
- **File:** `configs/simulator.yaml`, `configs/episodes.yaml`, `simulator/config.py`; bổ sung chi tiết hợp đồng tại `data.md` nếu cần.
- **Triển khai:** định nghĩa config cho thế giới, thời gian, hành vi giao dịch, fraud, reporting delay và quy mô. Tách tham số nghiệp vụ khỏi seed và số lượng của profile smoke/pilot. Khai báo rõ tham số nào có nguồn, tham số nào là giả định mô phỏng; không gán tên nguồn cho một con số chưa được nguồn hỗ trợ. Chốt semantics của timestamp/timezone, đơn vị tiền và cách tham chiếu lịch sử chung theo episode trong `data.md`.
- **Kiểm tra:** config thiếu trường bắt buộc, probability ngoài miền, lịch thời gian không hợp lệ hoặc giá trị không có đơn vị/provenance phải được báo rõ. Profile smoke giảm quy mô, không đổi ý nghĩa của protocol.
- **Hoàn tất:** config đọc được và validate được; các giá trị chưa có nguồn mang nhãn assumption thay vì bị bỏ trống hoặc chặn vô thời hạn. Không mở thêm vòng literature review rộng.

### P1.2 — Schema, storage và fixture nhỏ (`todo`)

- **Mục tiêu:** có fixture thủ công rất nhỏ chạy xuyên qua bước ghi/đọc dữ liệu và snapshot as-of.
- **Phụ thuộc:** P1.1; hợp đồng dữ liệu ở `data.md` được cụ thể hóa đủ để tạo bảng.
- **File:** `simulator/schema.py`, `storage.py`, `asof.py`; fixture và các test integrity/as-of.
- **Triển khai:** tạo schema, khóa và quan hệ; export operational/private; xây API đọc theo as-of và episode. Phân loại từng trường thành dữ liệu quan sát được, nhãn đã khả dụng hoặc metadata/truth nội bộ. Vai trò fraud ẩn, ID template/seed và đáp án injector không được đưa vào feature operational. Dựng fixture gồm nhiều episode, dữ liệu về trễ và nhãn chưa về để kiểm tra ranh giới trước khi viết generator lớn.
- **Kiểm tra:** khóa trùng, FK mồ côi, truy vấn nhầm episode, bản ghi chưa khả dụng, thời điểm đúng bằng cutoff; thêm dữ liệu chỉ khả dụng sau t không đổi snapshot tại t. Liệt kê bảng/cột/export của operational để xác nhận không có truth nội bộ.
- **Hoàn tất:** fixture ghi/đọc được, trả đúng các hàng theo episode/as-of và không lẫn truth. Không coi một DB nằm ở thư mục khác là bằng chứng cô lập tool end-to-end.

### P1.3 — Thế giới và giao dịch hợp lệ (`todo`)

- **Mục tiêu:** sinh lịch sử nền nhất quán từ profile khách hàng/merchant, gồm hành vi ngày/giờ/kênh theo config.
- **Phụ thuộc:** P1.2.
- **File:** `simulator/world.py`, `transactions.py`; bổ sung test integrity và reproducibility.
- **Triển khai:** sinh entity, mạng quan hệ và luồng giao dịch hợp lệ theo nghiệp vụ trong `data.md`. Giữ riêng RNG/seed của thế giới, giao dịch và các thành phần sẽ bổ sung để thay đổi một kịch bản không làm đổi lịch sử nền ngoài ý muốn. Lịch nghiệp vụ công khai chỉ chứa kỳ vọng đã khai báo; không trích đáp án hậu kiểm từ truth.
- **Kiểm tra:** quan hệ entity, tính hợp lệ mốc thời gian, trạng thái/response, đơn vị tiền và quan hệ refund/reversal theo hợp đồng đã chốt. Cùng seed/config cho cùng nội dung dữ liệu khi sort theo khóa; đổi seed tạo giao dịch khác nhưng vẫn đạt bất biến.
- **Hoàn tất:** profile smoke sinh được giao dịch hợp lệ và bảng phân phối nền. Có cơ chế lưu fingerprint của nội dung canonical; không yêu cầu file DB nhị phân có hash giống nhau nếu metadata vật lý khác.

### P1.4 — Hành vi fraud và các ca đối chứng (`todo`)

- **Mục tiêu:** hiện thực các cơ chế fraud và đối chứng nghiệp vụ đã có trong MVP mà không đưa nhãn ẩn vào feature.
- **Phụ thuộc:** P1.3.
- **File:** `simulator/fraud.py`; test integrity/incidents và báo cáo diagnostic.
- **Triển khai:** các cơ chế, vai trò người nhận, cash-out và `legit_business_spike` theo `data.md`. Tách metadata generator khỏi thuộc tính quan sát được. Các đặc trưng phía người nhận phải suy ra được từ giao dịch đã quan sát, không ghi sẵn từ truth; định nghĩa rõ cửa sổ chưa hoàn tất.
- **Kiểm tra:** fraud/benign đối chứng đều đi qua cùng schema và luồng ghi dữ liệu; không có cột operational mã hóa trực tiếp cơ chế hoặc nhãn. Kiểm tra quan hệ chuỗi giao dịch và cash-out; ca fraud pha trộn với benign vẫn nhất quán.
- **Hoàn tất:** từng cơ chế sinh được ví dụ có thể kiểm tra bằng dữ liệu; báo diagnostic artefact và giới hạn simulator. Một feature phân biệt mạnh phải được giải thích từ hành vi/dữ liệu khả dụng, không tự coi AUC cao hay thấp là bằng chứng duy nhất về leakage.

### P1.5 — Label events, snapshot và label tail (`todo`)

- **Mục tiêu:** nhãn quan sát được xuất hiện đúng nguồn và đúng thời điểm; pending không bị chuyển thành âm trước policy.
- **Phụ thuộc:** P1.4; fixture snapshot của P1.2.
- **File:** `simulator/labels.py`; `tests/test_simulator_labels.py`, test as-of.
- **Triển khai:** sinh reporting và độ trễ từ config; snapshot theo trạng thái MVP trong `data.md`. Giữ khả năng fraud chưa bao giờ được báo trong truth riêng. Label tail không tạo thêm giao dịch cho feature. Chỉ mô phỏng feedback điều tra viên theo cơ chế được khai báo; nếu phụ thuộc alert model thực tế thì chờ P2/P3, không giả gọi model chưa có.
- **Kiểm tra:** ngay trước/đúng mốc trưởng thành; nhãn reported nhưng chưa available; fraud về sau H; fraud không được báo; nguồn nhãn phù hợp kênh; mỗi giao dịch tối đa một kết luận MVP. Quan sát nhãn dài hơn không làm thay đổi feature của thời điểm quá khứ.
- **Hoàn tất:** bảng snapshot và báo cáo độ trễ đúng trên fixture và smoke; tách số quan sát được khỏi số do oracle evaluator biết. Không triển khai sửa nhãn/disputed thuộc phần mở rộng.

### P1.6 — Injector, calendar và truth sự cố (`todo`)

- **Mục tiêu:** mỗi sự cố có tác động kiểm tra được và đáp án phục vụ evaluator, không tự quyết định alert/agent phải kết luận gì.
- **Phụ thuộc:** P1.4–P1.5.
- **File:** `simulator/incidents.py`; `tests/test_simulator_incidents.py`.
- **Triển khai:** các template và ca kết hợp theo `data.md`; ghi truth vào nguồn private. Calendar ghi kỳ vọng nghiệp vụ theo hợp đồng metric. Giữ nguyên lịch sử trước T0. Các mốc can thiệp, deadline và hành động chấp nhận được lấy từ hợp đồng nguồn, không tự tạo taxonomy khác trong code.
- **Kiểm tra:** seasonal một ngày trên nền không đều; pipeline đổi đơn vị/NULL; fraud surge/new fraud; đối chứng hợp lệ; hai nguyên nhân. So với bản đối chứng cùng seed để xác nhận phần bị tác động, giữ nguyên phần được yêu cầu không đổi. `effect_available_at` xuất phát từ bản ghi tác động thực tế, không chỉ từ timestamp ghi trong config; sửa một bản ghi sẵn có cũng phải phản ánh lúc thay đổi trở nên khả dụng.
- **Hoàn tất:** mọi template có fixture chứng minh tác động và truth đúng. Test chỉ khẳng định dữ liệu thay đổi đúng cơ chế; việc monitoring bắt được hay không thuộc P3, không ép mọi ca phải tạo alert ở P1.

### P1.7 — Episode và manifest (`todo`)

- **Mục tiêu:** sinh được bộ episode dùng chung cho mọi baseline, khóa vai trò trước tuning.
- **Phụ thuộc:** P1.6.
- **File:** `simulator/episodes.py`, `configs/episodes.yaml`; `tests/test_simulator_episodes.py`, test reproducibility.
- **Triển khai:** cùng thế giới/lịch sử trước T0, các tương lai riêng theo seed. Cách xuất/đọc lịch sử chung phải tuân theo biểu diễn đã chốt ở P1.1–P1.2; không cần mỗi episode có một bản vật lý riêng nếu snapshot vẫn cách ly đúng. Sinh manifest với vai trò theo `evaluation.md`; bản tối thiểu theo MASTER, cỡ benchmark cuối khóa sau pilot chi phí. Nhãn lịch mặc định của dòng demo không tự lọt vào episode danh nghĩa.
- **Kiểm tra:** thêm/bỏ/reorder episode không đổi dữ liệu của episode có cùng seed/config; role và seed tái lập được; các tập tách ở cấp episode; episode hai nguyên nhân vẫn là một đơn vị chia tập; không có giao dịch từ episode khác trong truy vấn.
- **Hoàn tất:** manifest có fingerprint và cấu hình đủ để tái tạo. P1 không dùng test để chỉnh tham số cho P2/P3/P4; xem lại test để tune khiến seed đó không còn held-out và phải sinh seed mới theo protocol.

### P1.8 — CLI, validation và bàn giao P2/P3 (`todo`)

- **Mục tiêu:** sinh và kiểm tra dữ liệu từ CLI, trả mã lỗi khi vi phạm hợp đồng; bàn giao đủ thông tin để P2/P3 dùng được.
- **Phụ thuộc:** P1.1–P1.7.
- **File:** `simulator/__main__.py`, `validate.py`; test reproducibility; báo cáo trong artifact run.
- **Triển khai:** CLI generate/validate; lưu config đã resolve, seed, phiên bản schema/code, fingerprint, thời gian chạy và kết quả kiểm tra. Tạo báo cáo support cho các cohort của P2, phân phối theo kênh/thời gian, nhãn trễ và độ đầy đủ cửa sổ. Báo cáo có dùng truth là private, không cấp cho tools.
- **Kiểm tra:** chạy toàn bộ smoke tests, sinh lại cùng config để đối chiếu canonical data; chạy pilot scale và kiểm tra support của các tập P2. Khi thiếu positive/reference, dừng bàn giao tập đó và ghi rõ nguyên nhân; không tự đổi split hoặc dùng oracle label để làm kết quả đẹp.
- **Hoàn tất:** CLI và các test runtime thực sự chạy đạt; P2/P3 nhận được artifact đúng contract và các giới hạn đã ghi. Cập nhật trạng thái MASTER và lệnh kiểm tra module trong AGENTS khi runtime tồn tại.

## 5. Lệnh mục tiêu và cách chạy theo từng mốc

Các lệnh dưới là **interface dự kiến sau khi implement**, hiện chưa chạy được vì chưa có package simulator. Khi CLI hoàn thành, sửa mục này theo lệnh thực tế.

```bash
python3 -m simulator generate --config configs/simulator.yaml --profile smoke --output data/p1-smoke
python3 -m simulator validate --dataset data/p1-smoke
python3 -m pytest tests/test_simulator_integrity.py tests/test_simulator_asof.py tests/test_simulator_labels.py
python3 -m pytest tests/test_simulator_incidents.py tests/test_simulator_episodes.py tests/test_simulator_reproducibility.py
python3 -m simulator generate --config configs/simulator.yaml --profile pilot --output data/p1-pilot
python3 -m simulator validate --dataset data/p1-pilot
```

- Mốc đầu: P1.1–P1.2, config + fixture nhỏ và kiểm tra as-of, chưa cần volume lớn.
- Mốc tiếp: P1.3–P1.6, kiểm tra từng cơ chế trên smoke trước khi ghép thành benchmark.
- Mốc bàn giao: P1.7–P1.8, manifest và pilot export có báo cáo đầy đủ.
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

**P1.1 — cấu hình và provenance.** Đầu ra đầu tiên là config có thể validate, các quyết định biểu diễn dữ liệu được ghi tại nguồn và danh sách giả định còn lại. Sau đó làm fixture của P1.2; chưa đánh dấu task nào done từ việc chỉ có sub-plan này.
