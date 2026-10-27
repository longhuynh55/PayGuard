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
- **Vòng 1 đã bàn giao:** [`evaluation/`](../evaluation/) đọc truth và manifest từ private DB, ghép P3.3 theo thời gian + segment + feature, và ghi report vào thư mục private. Thời điểm phát hiện lấy từ `available_at` của snapshot có `alert_ids_json` tăng; snapshot chỉ đổi quyết định không kéo dài khoảng phát hiện. Mapping feature được khai báo tường minh trong matcher; segment `all` được hiểu là metric tổng hợp bao phủ các segment truth đã khai báo. `ambiguous_match` và hiệu ứng trễ cửa sổ đầy đủ vẫn để vòng 2 theo bảng phạm vi phía trên. Smoke P1→P3: detection recall tổng **2/2 = 1.0** (`data_pipeline` **2/2 = 1.0**; `seasonal` mẫu số **0 → NA**), **3** incident unmatched, **0** incident khớp truth benign. Đây là số smoke của pipeline hiện tại, **không phải kết quả đánh giá**.
- **Unmatched của smoke:** `incident-c89881b3dc8c2d6e9ebd1354` (`ep-0001`, clean) không có truth sự cố trong episode; `incident-fff68272d2477ac4092c8e68` (`ep-0003`) và `incident-bb87bc0d151aa61383cb4aec` (`ep-0005`) có alert `amount_avg_7d`/segment `all`, trong khi truth seasonal khai báo feature `txn_count`/segment `retail`. `all` bao phủ `retail`, nhưng feature khác nhau; mapping `amount_avg_7d → amount` là đúng theo feature bị tác động, không ánh xạ sang `txn_count`. Các alert seasonal khả dụng 07–11/02, ngoài effect windows 04–05/02; cửa sổ hiệu ứng trễ đầy đủ thuộc vòng 2.

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

- **Backlog P3.6 (không chặn, [findings r1](../review/findings/P3.6-r1.md)):** `load_episode_config` gọi `load_simulator_config()` với đường dẫn tương đối (phụ thuộc thư mục chạy; `generate --config X` đối chiếu file mặc định) → chuyển kiểm tra khớp `duration_days` sang `simulator generate`/`validate`; `model score`/`model evaluate` (P2) vẫn nạp `configs/episodes.yaml` chỉ để lấy `duration_days` → chuyển sang khóa công khai.

## Nghiệm thu P3
- [x] Metric/alert/incident đúng as-of và tái lập được.
- [x] Incident con hoạt động đúng; không mở lại incident đã quyết định.
- [x] Evaluator là nơi duy nhất đọc truth; detection recall đối soát được.
- [x] Test runtime xanh.
