# P3 — Sub-plan triển khai monitoring

> Trạng thái: **P3.1–P3.4 round1-done (P3 vòng 1 đã bàn giao)**. Hợp đồng: [monitoring.md](monitoring.md); đối soát recall: [evaluation.md](evaluation.md). File này sở hữu thứ tự task và nghiệm thu P3; không chép hợp đồng.
> Format task và luật hai vòng giống [p1-implementation.md](p1-implementation.md). Điều phối: [orchestration.md](orchestration.md).

## Thứ tự hai vòng

| Task | Vòng 1 — luồng mỏng | Vòng 2 |
|---|---|---|
| P3.1 Job hằng ngày + metric | Một nhóm metric: PSI trên vài feature + tỉ lệ review của model | KS, phân phối score/uncertainty, feature phía người nhận, CBPE |
| P3.2 Signal + alert | Ngưỡng khởi đầu (PSI > 0,25) | Benjamini–Hochberg; chỉnh ngưỡng trên clean development |
| P3.3 Incident + incident con | Đầy đủ | — |
| P3.4 Ghép truth + detection recall | Matcher tối thiểu (thời gian + segment + feature) | `ambiguous_match`, hiệu ứng trễ cửa sổ đầy đủ |

P5.2 Fix 1 (31/10): các phần hoãn trong P3.4 đã được triển khai; quy tắc matcher hoàn chỉnh thuộc [evaluation.md](evaluation.md), mục 2, và nghiệm thu tại [p5-implementation.md](p5-implementation.md), mục P5.2.

## Bố trí file dự kiến

```text
monitoring/   __init__.py, metrics.py, signals.py, incidents.py, __main__.py
evaluation/   __init__.py, matcher.py, recall.py          # evaluator: module duy nhất đọc truth
tests/        test_monitoring_metrics.py, test_monitoring_incidents.py, test_evaluation_recall.py
```

## Các task

### P3.1 — Job hằng ngày và metric (`round1-done` — Vòng 1 đã bàn giao)
- **Mục tiêu:** với mỗi episode, mỗi ngày nghiệp vụ (giờ VN) tính metric cửa sổ 7 ngày so với reference 11/2025–1/2026.
- **Phụ thuộc:** P2.5 (`model_scores`), artifact P1.
- **Kiểm tra:** metric tại ngày d chỉ dùng dữ liệu `available_at <= as_of_time` của d; cùng input → cùng output.
- **Hoàn tất:** bảng metric theo ngày cho smoke.
- **Vòng 1 đã bàn giao:** `model.review_rate` hiện chỉ là tỷ lệ thô vì `model_scores` chưa được chấm trên giai đoạn reference; metric này chưa dùng để sinh signal và phần reference được để sang vòng 2.

### P3.2 — Signal và alert (`round1-done`)
- **Mục tiêu:** chuyển metric vượt ngưỡng thành `alerts` theo schema [monitoring.md](monitoring.md).
- **Phụ thuộc:** P3.1.
- **Kiểm tra:** episode clean smoke không bùng alert hàng loạt; kịch bản đổi đơn vị P1 sinh alert trên feature bị tác động. Ngưỡng KHÔNG tune trên episode test.
- **Hoàn tất:** bảng `alerts` tái lập được.
- **Vòng 1 đã bàn giao:** [`monitoring/signals.py`](../monitoring/signals.py) nhận các dòng P3.1 trong bộ nhớ theo đúng episode, chỉ xét snapshot `as_of_time <=` cutoff; PSI dùng phép so sánh nghiêm ngặt `> 0,25` theo config/provenance ở [`configs/monitoring.yaml`](../configs/monitoring.yaml). Metric censored, thiếu mẫu, cửa sổ chưa đủ hoặc thiếu reference bị chặn và ghi lý do trong report; `model.review_rate` không sinh signal vì chưa có reference. Smoke 30 ngày ghi số alert: `ep-0001` (clean) **1**, `ep-0002` (data_pipeline) **1**, `ep-0003` (seasonal) **5**, `ep-0004` (data_pipeline) **1**, `ep-0005` (seasonal) **5**; clean dưới giới hạn smoke 5 và data_pipeline có alert `amount_avg_7d` sau effect. `daily_metrics`/`alerts` chưa là bảng operational đọc qua `read_as_of`; lưu thành bảng operational và thêm allowlist as-of là việc vòng 2 hoặc trước P4.

### P3.3 — Incident và incident con (`round1-done` — Vòng 1 đã bàn giao)
- **Mục tiêu:** gộp alert vào incident đang mở; incident đã có quyết định cuối → incident con `parent_incident_id`.
- **Phụ thuộc:** P3.2.
- **Kiểm tra:** gộp trong ≤ 2 ngày cùng feature/segment; không gộp khác segment; incident đã đóng không bị mở lại. Ở P3 chưa có triage: test dùng quyết định giả lập.
- **Hoàn tất:** bảng `incidents` + test.
- **Vòng 1 đã bàn giao:** [`monitoring/incidents.py`](../monitoring/incidents.py) nhận alert P3.2 cho một episode và một `DecisionSource`; trạng thái được xuất thành snapshot có `event_at`/`available_at`, ID trung tính tất định và CLI CSV `python -m monitoring incidents`. Test khóa mốc ≤ 2 ngày từ alert gần nhất cho cả gộp incident mở lẫn liên kết incident con. Smoke 30 ngày có số incident theo episode: `ep-0001` (clean) **1**, `ep-0002` (data_pipeline) **1**, `ep-0003` (seasonal, 5 alert) **1**, `ep-0004` (data_pipeline) **1**, `ep-0005` (seasonal, 5 alert) **1**.

### P3.4 — Ghép truth và detection recall (`round1-done`)
- **Mục tiêu:** evaluator ghép incident với bảng truth, tính detection recall trên mọi sự cố cần can thiệp.
- **Phụ thuộc:** P3.3, truth của P1.
- **Kiểm tra:** `unmatched` không tự thành benign; sự cố không có alert tính là bỏ sót; mẫu số 0 → NA. Monitoring/model KHÔNG import module evaluator.
- **Hoàn tất:** báo cáo detection recall theo kịch bản cho smoke; cập nhật Trạng thái MASTER. Vòng 1 xong P3 = đủ đầu vào cho mốc ★ (rule + gate + agent một bước thuộc P4).
- **Vòng 1 đã bàn giao:** [`evaluation/`](../evaluation/) đọc truth và manifest từ private DB, ghép P3.3 theo thời gian + segment + feature, và ghi report vào thư mục private. Thời điểm phát hiện lấy từ `available_at` của snapshot có `alert_ids_json` tăng; snapshot chỉ đổi quyết định không kéo dài khoảng phát hiện. Mapping feature được khai báo tường minh trong matcher; segment `all` được hiểu là metric tổng hợp bao phủ các segment truth đã khai báo. `ambiguous_match` và hiệu ứng trễ cửa sổ đầy đủ vẫn để vòng 2 theo bảng phạm vi phía trên. Smoke P1→P3: detection recall tổng **2/2 = 1.0** (`data_pipeline` **2/2 = 1.0**; `seasonal` mẫu số **0 → NA**), **3** incident unmatched, **0** incident khớp truth benign. Đây là số smoke của pipeline hiện tại, **không phải kết quả đánh giá**. P5.2 Fix 1 (31/10) hoàn tất các phần matcher đã hoãn; báo cáo phiên bản mới và hành vi candidate được định nghĩa trong [data.md](data.md) và [evaluation.md](evaluation.md), mục 2.
- **Unmatched của smoke:** `incident-c89881b3dc8c2d6e9ebd1354` (`ep-0001`, clean) không có truth sự cố trong episode; `incident-fff68272d2477ac4092c8e68` (`ep-0003`) và `incident-bb87bc0d151aa61383cb4aec` (`ep-0005`) có alert `amount_avg_7d`/segment `all`, trong khi truth seasonal khai báo feature `txn_count`/segment `retail`. `all` bao phủ `retail`, nhưng feature khác nhau; mapping `amount_avg_7d → amount` là đúng theo feature bị tác động, không ánh xạ sang `txn_count`. Các alert seasonal khả dụng 07–11/02, ngoài effect windows 04–05/02; cửa sổ hiệu ứng trễ đầy đủ thuộc vòng 2. P5.2 Fix 1 (31/10) ghi nhận đây là unmatched vì khác feature; cửa sổ thời gian nay được tính theo metric window cộng feature lookback theo [evaluation.md](evaluation.md), mục 2. Sau merge P1.3 round 2, smoke có 9 incidents: **4 matched / 0 ambiguous / 5 unmatched**, detection recall **2/2**. P5.2 giữ các số này; test expectation cũ `matched == 2` được sửa thành `matched == 4`. Incident unmatched thuộc seasonal vẫn do feature `txn_count` không khớp; clean unmatched không có truth.

### P3.5 — Lưu đầu ra monitoring vào operational store (`round1-done` — Vòng 1 đã bàn giao)
- **Mục tiêu:** `daily_metrics`, `alerts`, `incidents` thành bảng SQLite trong DB operational, đọc qua `read_as_of`, để P4 ([triage.md](triage.md)) lấy incident/alert tại `as_of_time` mà không đọc CSV hay truth.
- **Phụ thuộc:** P3.1–P3.3, P1.2 (`read_as_of`).
- **Phạm vi:** DDL operational + thêm 3 bảng vào allowlist as-of; CLI `python -m monitoring` ghi vào DB operational (giữ xuất CSV hiện có); `daily_metrics` thêm `event_at`/`available_at` bằng `as_of_time` (cùng quy ước `alerts`). Schema sửa tại [data.md](data.md) mục 2 và 7 trong cùng thay đổi. KHÔNG đổi logic tính metric, ngưỡng, quy tắc gộp incident; KHÔNG lưu gì private.
- **Kiểm tra:** đọc qua `read_as_of` tại t không thấy dòng có `available_at > t` hay của episode khác; dữ liệu ghi vào DB khớp từng dòng với output in-memory/CSV vòng 1; chạy lại cùng input → nội dung bảng y hệt; bảng không có cột/giá trị truth (test kiểm tra DDL và ID trung tính như các bảng operational khác); `simulator validate` vẫn đạt; evaluator P3.4 cho cùng số recall smoke.
- **Hoàn tất:** smoke P1→P3 ghi 3 bảng vào DB operational; test as-of + tái lập; cập nhật Trạng thái MASTER.
- **Vòng 1 đã bàn giao:** DDL và `read_as_of` allowlist có đủ ba bảng; CLI `metrics`, `signals`, `incidents` lưu snapshot operational và vẫn xuất CSV. Metric giữ nguyên phép tính, thêm `event_at`/`available_at = as_of_time`; ghi lặp cùng khóa chỉ chấp nhận payload giống hệt. Smoke `simulator generate` → P2 train/score → ba CLI monitoring → `simulator validate` đạt khi `model_scores`, `daily_metrics`, `alerts`, `incidents` đã có dữ liệu. Validator đối chiếu schema/khóa/FK, as-of theo episode, quan hệ alert-metric-incident, timestamp snapshot, ID băm trung tính và ranh giới truth; test làm lệch `available_at` hoặc chuyển alert sang episode khác đều bị từ chối. Test P3.4 giữ nguyên recall smoke **2/2 = 1.0**.

### P3.6 — Tách tham số vận hành khỏi cấu hình injector (`round1-done` — Vòng 1 đã bàn giao)
- **Mục tiêu:** monitoring (và mọi module ngoài simulator/evaluator) KHÔNG nạp `configs/episodes.yaml`, vì file này chứa mapping episode → template của injector (private theo [data.md](data.md)). Phát hiện ở review P4.3 r1: `monitoring/metrics.py` nạp file này chỉ để lấy `duration_days`.
- **Phụ thuộc:** P3.1, P4.3 (runner triage đã bỏ đường đọc này).
- **Phạm vi:** lấy độ dài episode (30 ngày, cố định theo hợp đồng MVP) từ một nguồn công khai — ví dụ cấu hình công khai không chứa template/seed/mapping, hoặc dữ liệu operational đọc qua `read_as_of`; simulator vẫn kiểm tra hai nguồn khớp nhau. KHÔNG đổi phép tính metric, ngưỡng, quy tắc gộp incident; KHÔNG đổi giá trị 30 ngày.
- **Kiểm tra:** test chặn loader `configs/episodes.yaml` (patch để raise) mà monitoring metrics/signals/incidents và `python -m triage run` vẫn chạy trên smoke; output metric/alert/incident smoke giống hệt trước khi sửa; `simulator validate` đạt.
- **Hoàn tất:** test + cập nhật Trạng thái MASTER.
- **Vòng 1 đã bàn giao:** `monitoring` lấy `episode.duration_days` từ cấu hình công khai có provenance trong [`configs/simulator.yaml`](../configs/simulator.yaml); simulator kiểm tra khóa này khớp `protocol.duration_days`. Monitoring không còn nhận hay nạp `configs/episodes.yaml`. Đối chiếu theo từng dòng trên smoke: `daily_metrics`, `alerts` và `incidents` giữ nguyên.

- **Backlog P3.6 (không chặn, findings r1):** `load_episode_config` gọi `load_simulator_config()` với đường dẫn tương đối (phụ thuộc thư mục chạy; `generate --config X` đối chiếu file mặc định) → chuyển kiểm tra khớp `duration_days` sang `simulator generate`/`validate`; `model score`/`model evaluate` (P2) vẫn nạp `configs/episodes.yaml` chỉ để lấy `duration_days` → chuyển sang khóa công khai. **Đã sửa ở BACKLOG-2:** loader episode thuần; generate/validate so khớp bằng các config đã nạp; model score/evaluate chỉ lấy duration từ config simulator công khai.

### P3.7 — Tính hợp lệ của monitoring (người dùng duyệt 31/10/2026, chen trước P5.0b)
- **Bối cảnh:** review P5.2 r1 (findings): (1) không có metric tổng lượng giao dịch theo ngày, nên sự cố seasonal (truth `txn_count`) không bao giờ ghép được và `seasonal_calendar` — runbook benign duy nhất — không có ca để chấm; (2) PSI `amount_avg_7d` vượt 0,25 ở 24/24 ngày cửa sổ đủ của mọi episode kể cả clean, alert chỉ bật/tắt do cổng `small_window_sample`, nên detection recall smoke 2/2 không chứng minh monitoring phát hiện lỗi pipeline.
- **Việc:** (a) chẩn đoán vì sao PSI `amount_avg_7d` luôn cao (reference lệch giai đoạn, Tết, cách bin, phân phối A2A/kênh…) và sửa; kiểm trên episode clean thuộc development. (b) thêm metric tổng lượng giao dịch theo ngày so với kỳ vọng `business_calendar`, ánh xạ với truth `txn_count`. Ngưỡng chỉnh trên clean development theo [monitoring.md](monitoring.md) mục 1, không đọc calibration/test. Metric mới PHẢI khai báo trong `FEATURE_LOOKBACK_DAYS` và `MONITORING_FEATURE_TO_TRUTH_FEATURE` của matcher (`evaluation/matcher.py`); thiếu thì lookback mặc định 0, không báo lỗi (P5.2 r2).
- **Nghiệm thu:** episode clean development không có alert nền hàng loạt; sự cố seasonal smoke sinh alert volume ghép được với truth; pipeline vẫn được phát hiện trên feature bị tác động; bảng cũ → mới cho alert/incident/recall smoke.
- **Bàn giao P3.7 round 1:** PSI spike đến từ cụm card-testing theo khách hàng với effective n nhỏ; đánh đổi độ nhạy khi giảm từ 10 xuống 2 bin và việc đánh giá lại ở vòng 2 được ghi tại [monitoring.md](monitoring.md), mục 1. Round 1 dùng hai quantile bins, một pseudocount và bin missing riêng; PSI sạch có một ngày vượt ngưỡng trước cổng sample nhưng bị chặn, nên clean phát 0 alert. Volume round 1 dùng weekday baseline và hai ngưỡng; Fix 1 thay đổi các lựa chọn này như ghi ở phần bàn giao bên dưới.
- **Smoke cũ → mới:** số cũ là smoke P5.2 trước P3.7 trên cùng profile; số mới đo lại sau P3.7 volume registration, đối soát bởi evaluator.

  | Chỉ số | Cũ | Mới |
  |---|---:|---:|
  | Alerts | 24 | 23 |
  | Incidents | 9 | 9 |
  | Incidents matched | 4 | 7 |
  | Ambiguous incidents | 0 | 0 |
  | Incidents unmatched | 5 | 2 |
  | Seasonal benign incidents matched | 0/2 | 2/2 |
  | Detection recall cho sự cố cần can thiệp | 2/2 (1.0) | 2/2 (1.0) |

  Clean development smoke `ep-0001` giảm từ 3 alert xuống 0; cả hai data-pipeline smoke vẫn có alert `amount_avg_7d` trên feature bị tác động. Round 1, hai seasonal smoke có volume alert `txn_count` vào 05/02 và đều match truth bằng cửa sổ matcher 7 ngày + lookback volume 1 ngày. Fix 1 thay cửa sổ volume thành 1+1 ngày; kết quả hiện tại ở phần bàn giao bên dưới.

- **Bàn giao P3.7 Fix 1:** baseline volume dùng median all-days của 92 ngày reference (4 giao dịch/ngày), thay vì weekday match, để xử lý lệch pha card-testing mỗi bảy ngày địa phương (thứ Ba ở base, Chủ nhật trong episode). Ngưỡng duy nhất khóa ở maximum `abs(actual - calendar expectation)` của 30 ngày clean development `ep-0001`: 4,25. Tuy nhiên seasonal 05/02 có residual 3,0 (`ep-0003`) và 3,5 (`ep-0005`), nhỏ hơn cực đại clean 4,25; không có ngưỡng chung vừa giữ sạch nền vừa phát hiện cả hai. Theo quyết định người dùng, không hạ ngưỡng; P3.7 Fix 1 bị blocked tại điều kiện seasonal-match. Matcher đọc `metric_windows` public: volume 1 ngày + lookback 1; `amount_avg_7d` 7+7; `txn_count_24h` 7+1. Feature thiếu public lookback vẫn bị từ chối bằng `EvaluationMatcherError` theo P5.2 r2. Chẩn đoán PSI, tài liệu, giá trị mặc định fixture và ghi chú bàn giao đã được cập nhật theo review P3.7-r1.

  | Smoke measure | Round 1 | Fix 1 |
  |---|---:|---:|
  | Alerts | 23 | 21 |
  | Incidents | 9 | 7 |
  | Matched | 7 | 5 |
  | Ambiguous | 0 | 0 |
  | Unmatched | 2 | 2 |
  | Benign matched | 2/2 | 0/2 |
  | Detection recall | 2/2 (1.0) | 2/2 (1.0) |
  | `data_ticket` IDs | `ep-0002/incident-67c06d4b51c5196eb2894cd0`; `ep-0004/incident-367c348143656924203a5bc3` | `ep-0002/incident-67c06d4b51c5196eb2894cd0`; `ep-0004/incident-367c348143656924203a5bc3` |

  Các ticket pipeline và invariant clean/seasonal giữ nguyên. Fix 1 giảm alert seasonal ở ngày sự kiện từ hai xuống không; `ep-0003` và `ep-0004` mỗi episode còn một volume alert nền tại T0 01/02 (đợt card-testing, residual 4,5 và 5,0), cả hai unmatched (`incident-6c9b3f…`, `incident-0cc327…`) — tính vào workload khi đếm P5.0b. Chi tiết phân phối/ngưỡng và lý do hoãn nằm tại [monitoring.md](monitoring.md), mục 1.

  **Quyết định người dùng 31/10:** seasonal-volume-match acceptance và benign support counting được defer tới pilot 60 / P1.6 vòng 2. Smoke hiện tại không phân biệt được residual seasonal 3,0 (`ep-0003`) và 3,5 (`ep-0005`) với clean max residual 4,25 (`ep-0001`); giữ ngưỡng 4,25.

### MON-RECAL — Hiệu chỉnh lại ngưỡng trên pilot development (người dùng duyệt 03/11/2026, sau SIM-SCALE)
- **Bối cảnh:** ngưỡng volume 4,25 và PSI 0,25 được chọn trên smoke clean `ep-0001`; trên pilot development (MON-TUNE-A) nhiễu clean volume max 0,201, PSI max 0,0098 — ngưỡng cao ~21–26 lần nhiễu. Quyết định 31/10 "không hạ ngưỡng có điều kiện" áp cho smoke; đây là áp lại **cùng quy tắc đã ghi** ([monitoring.md](monitoring.md) mục 1: vượt nghiêm ngặt max clean development) trên dữ liệu đúng quy mô.
- **Việc:** ngưỡng `volume_relative_change_threshold` và `psi_threshold` theo **profile** (smoke giữ 4,25/0,25; pilot hiệu chỉnh trên 4 episode clean development của pilot sau SIM-SCALE), khóa trước calibration/test; ghi giá trị, thống kê và provenance trong `configs/monitoring.yaml` và monitoring.md. Không đổi số bin, cửa sổ, `minimum_sample_count`.
- **Nghiệm thu:** bảng development (chỉ development): phát hiện theo truth, alert trên clean = 0 theo cấu trúc (ghi rõ in-sample), alert benign; chạy lại harness pilot + P5.0b.
- **Quyết định đi kèm (03/11):** velocity/cửa sổ dưới ngày cho `fraud_surge` và feature phía người nhận cho `new_fraud` để P3 vòng 2, ghi kết quả âm tính (mục 3 monitoring.md cho phép). Luật gộp incident §2 giữ (22 incident con ở `ep-0009` đúng hợp đồng; báo như workload). Matcher ghép theo feature + thời gian, không theo nguyên nhân: nêu rõ trong báo cáo recall.
- **Bàn giao MON-RECAL (vòng 1, 04/11/2026):** nhánh `slice/mon-recal` sau SIM-SCALE. Pilot sinh từ worktree tại `/var/tmp/payguard-mr/pilot` (fingerprint `24e6c2f7…`, validation 22/22). Dữ liệu, script và báo cáo JSON nằm ngoài repo; không có gì private trong repo.
  - **Cơ chế profile:** `signals.profiles.smoke` giữ nguyên 0,25 và 4,25 (giá trị và text); `signals.profiles.pilot` có ngưỡng riêng. `compute_alerts(..., profile=...)` và `python -m monitoring signals --profile {smoke,pilot}` bắt buộc; harness truyền profile của manifest vào `_prepare_model_monitoring`. Profile thiếu ngưỡng, ngưỡng khai báo ở cấp trên, hoặc profile lạ đều bị từ chối, không mượn profile khác. `configs/monitoring.yaml` nằm trong `CONFIG_FILES` của định danh run, nên ngưỡng pilot nằm trong hash định danh (đối chiếu bằng đọc mã, không có test riêng).
  - **Backlog (không chặn):** CLI `python -m monitoring signals` không đối chiếu `--profile` với run; người gọi phải truyền đúng profile của run.
  - **Hiệu chỉnh (4 episode clean development `ep-0001`–`ep-0004`):** chọn từ manifest khóa (`configs/episodes.yaml`, `profiles.pilot.assignments`, role = development và template = clean), đối chiếu `episodes.role` trong DB, rồi mới đọc dòng metric; không đọc calibration/test. Kết quả: volume cực đại chính xác 0,20114942528735624 (ep-0002, 13/02), n = 120 → **0,21**; PSI 2 bin cực đại chính xác 0,009764810255898246 (`psi.amount_avg_7d`, ep-0002, 26/02), n = 192 (96 mỗi feature; loại 48 snapshot chưa đủ cửa sổ) → **0,01**; `psi.txn_count_24h` cực đại 0,0042357447696240065. Quy tắc: giá trị nhỏ nhất trên lưới 0,01 không nhỏ hơn cực đại; áp đúng quy tắc này lên cực đại smoke cho 4,25. Cách đọc "lớn hơn nghiêm ngặt" cho cùng hai số. Alert clean trong mẫu: 0 (in-sample theo cấu trúc, không phải kết quả ngoài mẫu). Calibration JSON: `/var/tmp/payguard-mr/calibration/pilot_calibration.json`.
  - **Smoke giữ nguyên (so với `main` 9aac9f0):** fingerprint `75df93e5…`; `reports/smoke.json` SHA `3d1703e1…`; `private/reports/smoke_truth.json` SHA `113f15e9…`; CLI `metrics`, `signals` (CSV và JSON báo cáo), `incidents` giống hệt từng byte; harness smoke `rule_human` và `rule_agent_fake`: `base_fingerprint` `d1c623…` giống nhau, 0 decisions cả hai (development của smoke là `ep-0001`, clean, không có incident). `run_fingerprint` đổi vì định danh có hash `configs/monitoring.yaml`; đây là hành vi định danh đã thiết kế.
  - **Bảng hiệu ứng development** (12 episode development, ngưỡng pilot đã khóa, không tune thêm; chỉ đọc development):

| Episode | Khuôn (manifest) | Alert (PSI amount / volume / PSI txn) | Incident (khớp / mơ hồ / không khớp) | Truth khớp | Alert trên clean | Alert trên truth benign |
|---|---|---|---|---|---|---|
| ep-0001 | clean | 0 (0 / 0 / 0) | 0 / 0 / 0 | — | 0 | — |
| ep-0002 | clean | 0 (0 / 0 / 0) | 0 / 0 / 0 | — | 0 | — |
| ep-0003 | clean | 0 (0 / 0 / 0) | 0 / 0 / 0 | — | 0 | — |
| ep-0004 | clean | 0 (0 / 0 / 0) | 0 / 0 / 0 | — | 0 | — |
| ep-0005 | two_cause_fraud_seasonal | 5 (4 / 1 / 0) | 2 / 0 / 0 | fraud_surge; seasonal (benign, cùng incident với fraud) | — | 1 (volume 05/02) |
| ep-0006 | two_cause_fraud_seasonal | 8 (7 / 1 / 0) | 2 / 0 / 0 | fraud_surge; seasonal (benign, cùng incident với fraud) | — | 1 (volume 05/02) |
| ep-0007 | fraud_calendar | 4 (3 / 1 / 0) | 2 / 0 / 0 | fraud_surge (fraud_calendar) | — | 0 |
| ep-0008 | seasonal (benign) | 0 (0 / 0 / 0) | 0 / 0 / 0 | — (truth benign không có alert) | — | 0 |
| ep-0009 | data_pipeline | 24 (24 / 0 / 0) | 1 / 0 / 0 | data_pipeline | — | 0 |
| ep-0010 | fraud_surge | 3 (2 / 1 / 0) | 2 / 0 / 0 | fraud_surge | — | 0 |
| ep-0011 | new_fraud | 1 (1 / 0 / 0) | 0 / 0 / 1 | — (new_fraud không khớp) | — | 0 |
| ep-0012 | legit_business_spike (benign) | 0 (0 / 0 / 0) | 0 / 0 / 0 | — | — | 0 |

  Tổng: **45 alert** (41 PSI `amount_avg_7d`, 4 volume, 0 `psi.txn_count_24h`); **10 incident**: 9 khớp, 0 mơ hồ, 1 không khớp; recall sự cố cần can thiệp **5/6 = 0,833** (bỏ sót `truth-ep-0011-new-fraud`); theo kịch bản: data_pipeline 1/1, fraud_surge (`fraud_surge`) 1/1, fraud_surge (`fraud_calendar`) 1/1, fraud_surge (`two_cause`) 2/2, new_fraud 0/1; incident chỉ khớp truth benign: **0**; alert trên clean: **0** (in-sample). Báo cáo evaluator: `/var/tmp/payguard-mr/effect/private/reports/p3_4_detection_recall.json`; bảng JSON: `/var/tmp/payguard-mr/effect/dev_effect_table.json`.
  - **Ghi chú cho orchestrator (cần xác nhận):**
    1. **Nguồn "manifest công khai có template":** repo không có manifest công khai chứa template; data.md ghi template là private, còn `episodes` chỉ có id và role. Tôi dùng `configs/episodes.yaml` (assignment khóa, cùng nguồn sinh `episode_manifest` private) để chọn episode, và đối chiếu role với DB. Cần xác nhận đây là nguồn đúng cho bước chọn.
    2. **Biên PSI mỏng:** ngưỡng 0,01 chỉ cao hơn cực đại clean 2,4%. Đây là kết quả của quy tắc, không tune. PSI `psi.txn_count_24h` không vượt 0,01 ở episode development nào; mọi PSI alert đến từ `amount_avg_7d`.
    3. **Alert volume 05/02 của ep-0005 và ep-0006:** hai alert nằm trong incident khớp đồng thời truth fraud và truth seasonal (benign) cùng `parent_scenario` `two_cause_fraud_seasonal`. Theo matcher, đó không phải incident chỉ-benign (`benign_matched` = 0), nhưng vẫn chạm cửa sổ truth benign; báo riêng để quyết định. Seasonal đơn lẻ (ep-0008) không có alert: lịch công khai khớp hiệu ứng sau SIM-SCALE.
    4. **new_fraud (ep-0011):** 1 PSI `amount_avg_7d` ngày 08/02 không ghép truth. Đây là kết quả âm tính đã ghi trong plan (feature phía người nhận → monitoring vòng 2).
    5. **ep-0009:** 24 alert gộp thành 1 incident vì chưa có quyết định. Không so trực tiếp với 22 incident của MON-TUNE-A (có quyết định `rule_human`).
    6. **Chưa chạy harness pilot và P5.0b** (nằm trong "Nghiệm thu" ở trên nhưng không thuộc task được giao): harness pilot sinh lại base trong ~33 phút và chấm vận hành trên mọi episode, gồm calibration và test. Chạy khi orchestrator yêu cầu.
  - **Kiểm tra đã chạy:** test đích (13 test `tests/test_monitoring_signals.py` và 6 test chạm đường gọi đã đổi, tất cả xanh); `simulator generate --profile smoke` và `validate` (21/21); `review/check_links.py`; `review/verify_plan_claims.py`; full suite một lần trên cây cuối: **476 passed, 1 xfailed**, exit 0 (xfail là test strict-xfail seasonal `test_monitoring_metrics.py` đã có sẵn, không đổi).
- **Trạng thái: merge 04/11/2026** (review Opus r1 PASS; F2 ghim ngưỡng pilot trong test, F4 đánh dấu câu PSI smoke đã sửa trước merge; full suite 476 passed + 1 xfailed). Pilot: volume 0,21 (max clean 0,20115, n=120), PSI 0,01 (max clean 0,009765, n=192). Development in-sample: 45 alert, 10 incident (9 matched, 1 unmatched ở `ep-0011`), 5/6 truth cần can thiệp; `new_fraud` sót (feature người nhận, vòng 2). Rủi ro (reviewer): biên PSI 2,4 % → ước 15–20 % episode clean held-out có ≥ 1 alert PSI giả; đo trên calibration, chỉ báo cáo, không tune lại. Ghi trong báo cáo: alert volume 05/02 ở `ep-0005`/`ep-0006` chạm cửa sổ truth seasonal benign nhưng cùng incident với truth fraud (F5). Backlog: CLI monitoring không đối chiếu `--profile` với run (F1). Nghiệm thu harness pilot + P5.0b: orchestrator chạy sau merge.

## Nghiệm thu P3
- [x] Metric/alert/incident đúng as-of và tái lập được.
- [x] Incident con hoạt động đúng; không mở lại incident đã quyết định.
- [x] Evaluator là nơi duy nhất đọc truth; detection recall đối soát được.
- [x] Test runtime xanh.
