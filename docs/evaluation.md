# Evaluation — metric, chấm điểm và thống kê

> Đầu vào: `decisions` ([triage.md](triage.md)), `incidents`/`alerts` ([monitoring.md](monitoring.md)), bảng truth sự cố ([data.md](data.md)). Evaluator là module **duy nhất** đọc truth.
> Stage xây: một phần ở P3 (đối soát recall), hoàn chỉnh ở P5.

## 1. Vai trò episode

- Pilot 60 episode: **12 development (4 clean) / 24 calibration (8 clean) / 24 test (8 clean)** ở cấp episode; stratify cơ chế theo mức support. Lưu manifest trước tuning. Nếu tăng số episode theo ngân sách, giữ tỉ lệ 1:2:2.
- **Development:** viết rule/prompt/tool/score, ngưỡng runbook, ngưỡng monitoring, matcher. **Calibration:** chỉ chọn quyền đóng từng runbook rồi tau agent ([triage.md](triage.md)). **Test:** chạy policy đã khóa một lần.
- Seed đã được xem trong lúc thiết kế thuộc development; sinh calibration/test seed mới.
- `rule_covered` khác `mechanism_seen_in_calibration`: seed mới cùng template đo giá trị ngoài rule, không chứng minh gặp cơ chế hoàn toàn mới. *Mở rộng:* stress test cơ chế giữ riêng.

## 2. Ghép incident với truth

- Dùng thời gian (gồm hiệu ứng trễ của cửa sổ 7 ngày), entity/segment và feature bị tác động.
- Trạng thái `matched` / `ambiguous_match` / `unmatched`. Không khớp **không** tự thành benign. Ambiguous và unmatched được đếm, giữ trong workload, không chấm accuracy. Matcher khóa trên development.
- *Mở rộng:* phân xử thủ công ca ambiguous (người phân xử không xem câu trả lời agent).

## 3. Chấm theo thời điểm quyết định

Gộp cả chuỗi alert rồi chấm bằng nhãn cuối sẽ phạt oan một quyết định đóng đúng ở ngày 1 (mùa vụ) vì fraud chỉ bắt đầu ngày 4 (phản ví dụ `decision_time_scoring` trong `review/`).

- Truth của một quyết định = các sự cố giao với phạm vi của nó và có `effect_available_at <= decision_at`. Quy ước bảo thủ: giao dịch chèn đầu tiên đã khả dụng là tính, kể cả khi chưa đủ mạnh để bật signal.
- Sự cố bắt đầu sau đó không phạt quyết định cũ; nó được tính vào recall qua incident con. Không có incident con → monitoring bỏ sót.
- Một unsafe closure đã xảy ra giữ nguyên dù incident con sau đó được routing đúng.

## 4. Metric

| Metric | Định nghĩa |
|---|---|
| Detection recall | Sự cố cần can thiệp có ít nhất một alert phù hợp / mọi sự cố cần can thiệp |
| **Timely routing recall** | Sự cố cần can thiệp được routing đúng hạn (dưới đây) / mọi sự cố cần can thiệp, kể cả monitoring bỏ sót |
| **Unsafe truth rate** (chính để so policy) | Số `incident_truth_id` cần can thiệp có ít nhất một unsafe closure / mọi `incident_truth_id` cần can thiệp trong tập đánh giá cố định |
| Unsafe episode rate | Episode có sự cố cần can thiệp và có ít nhất một unsafe closure / mọi episode có sự cố cần can thiệp trong cùng tập cố định |
| Dangerous closure rate (vận hành) | Incident cần can thiệp bị đóng / incident cần can thiệp tới triage |
| Unsafe fraction among closures | Incident đóng sai / mọi incident tự đóng |
| Benign automation | Incident benign đóng đúng / incident benign đủ điều kiện đánh giá |
| Workload | Số case, recheck, ticket; alert lặp; phút xử lý chỉ là giả định ghi rõ |
| Thời gian và chi phí | Từ lúc dữ liệu khả dụng tới quyết định; chi phí API toàn pipeline gồm cả recheck |

### Đơn vị so sánh không phụ thuộc cách policy chia case

Các policy dùng cùng manifest episode và cùng tập truth của injector; không lấy số incident tự sinh làm mẫu số rủi ro so sánh chính. Một sự cố truth có nhiều incident con/unsafe closure chỉ được tính **một lần** trong unsafe truth rate; episode cũng chỉ tính một lần trong unsafe episode rate. Truth có nhiều nguyên nhân thành phần dùng ID thành phần như [data.md](data.md), không gộp tùy theo kết luận của policy. Chỉ gắn unsafe closure cho truth đã khả dụng và giao phạm vi tại `decision_at` theo mục 3.

Số benign đóng đúng dùng để chọn policy trên calibration cũng đếm mỗi benign truth ID một lần. Quyết định chỉ được thưởng là benign đóng đúng khi đáp án của toàn phạm vi đã rõ và không có truth cần can thiệp đồng thời tại `decision_at`; không thưởng phần benign của một quyết định đóng sai ca nhiều nguyên nhân. Không dùng ca unmatched/ambiguous để tăng reward. Reward của routing đúng và cách xử lý lặp: mục 5.

Ví dụ cùng một sự cố có một lần đóng sai: policy tạo 7 incident cho dangerous closure rate 1/7 = 14,29%, policy tạo 3 incident cho 1/3 = 33,33%. Cả hai đều có unsafe truth rate = 1/1. Giữ tỷ lệ incident để mô tả vận hành, nhưng không dùng riêng nó để kết luận policy nào an toàn hơn. Benign automation theo incident cũng là metric vận hành; so giảm tải cần workload/chi phí trên cùng tập episode, kèm số case con và alert lặp.

Không có alert không tính là unsafe closure; vẫn là bỏ sót trong detection/timely-routing recall. Luôn báo các metric này cùng nhau để policy không phát alert/không đóng không được diễn giải thành thành công nghiệp vụ.

Nếu có closure `ambiguous_match` / `unmatched` chưa phân xử được truth: báo số lượng theo policy, nguồn và độ bao phủ đánh giá. Chỉ tính được unsafe truth/episode rate quan sát trên các liên kết xác nhận thì đánh dấu là **cận dưới quan sát**, không gán closure chưa biết là benign và không gọi zero quan sát là không có lỗi. Nếu có tập truth ứng viên, báo thêm kịch bản bảo thủ tính mọi liên kết ứng viên nguy hiểm là unsafe; thiếu tập ứng viên thì không tuyên bố cận trên đầy đủ. Các ca này vẫn trong workload/chi phí.

**Routing đúng hạn:** có ít nhất một quyết định trên incident ghép với sự cố (kể cả incident con) thỏa đồng thời:
1. action ∈ `accepted_actions` của sự cố;
2. `intervention_start_at <= decision_at <= deadline_at`.

Quyết định trên incident không ghép với sự cố, hoặc trước `intervention_start_at`, không được tính. `close` không bao giờ đạt; `recheck` là chưa giải quyết. Báo tách *routing tự động đúng đội* (`fraud_ops`/`data_ticket`) khỏi *routing an toàn* (`human_review`). D = 7 ngày, sensitivity D ∈ {3, 7, 14}. Routing đúng chưa đồng nghĩa người đã xử lý thành công.

Metric phụ: accuracy nguyên nhân (chỉ ca truth rõ, tách known/unknown, công khai ca không chấm được) · risk–coverage · thời gian phát hiện. Case có hạn recheck vượt cuối episode ghi censored/unresolved.

### Giá trị agent theo đúng phạm vi MVP

- Báo tổng số case và số truth duy nhất theo nguồn quyết định rule/agent; tách agent routing, agent tự đóng, recheck và chuyển người. Đo chất lượng chẩn đoán/routing trên phần ca agent thực sự nhận, kèm mẫu số và nhóm known/unknown.
- Báo riêng ca benign được evaluator xác nhận nhưng không có runbook benign áp dụng: đây là giới hạn quyền tự đóng của MVP ([triage.md](triage.md)), không tự suy ra agent chẩn đoán sai. Lý do không đóng phải có trong kết quả checklist/quyết định.
- So rule+human với hybrid trên toàn bộ tập episode; kết quả trên nhánh agent chỉ giải thích agent có ích ở đâu, không thay thế kết quả end-to-end. Các bảng này không chứng minh agent tự xử lý được benign ngoài runbook hoặc tiết kiệm phút lao động thực tế.

## 5. Utility (điểm giả định)

Benign đóng đúng +1; routing data_ticket/fraud_ops đúng đội +1; fraud chuyển nhầm sang data −3; đóng nhầm fraud −5; đóng nhầm pipeline −3; benign tạo ticket sai −1; human_review/recheck 0. Reward +1 chỉ tính một lần cho mỗi sự cố truth và cùng loại kết quả, không cộng thêm vì tạo incident con/routing lặp; chi phí và phạt của từng hành động sai vẫn giữ. Chỉ chấm ca có đáp án rõ, công khai số ca chưa chấm được. Nhân mức phạt với c ∈ {1, 2, 5}. Đây là điểm giả định, không phải tiền tiết kiệm. Không gán mọi alert về nguyên nhân nghiêm trọng nhất chỉ do chồng thời gian.

## 6. Baseline và hai phép so sánh agent

Cùng monitoring, snapshot, dữ liệu as-of:
**rule + human (baseline chính)** · rule + agent không UQ · rule + agent có UQ · luôn chuyển người.

| So sánh | Nhánh A | Nhánh B | Trả lời |
|---|---|---|---|
| **Tác dụng gate UQ** | Cùng kết luận đa số và bằng chứng của 3 trace với B; **bỏ** điều kiện `close_score >= tau_agent` | Giữ điều kiện đó | Riêng điều kiện score có giảm đóng nhầm không |
| **Giá trị vận hành** | Agent một lượt chạy thật, K = N = 1 | Hệ thống đầy đủ K = N = 3 | Chất lượng tăng có đáng chi phí không |

- Hai nhánh qua cùng checklist, cùng quyền đóng của rule; chỉ khác điều kiện score (so sánh 1) hoặc ngân sách tính toán (so sánh 2).
- Đa số benign tương đương `v_benign >= 2/3`: khi `tau_agent = 2/3`, A và B chỉ khác qua thành phần SQL consistency; khi `tau_agent = 1`, B còn đòi đồng thuận tuyệt đối. Nếu tau = 2/3 và thành phần SQL không bao giờ chặn thì A ≡ B — đó là kết quả cần báo cáo.
- So sánh 1 không tốn thêm lời gọi LLM; so sánh 2 tính vào ngân sách.
- Kết luận chấp nhận được cả hai chiều: nếu agent không hơn rule, báo cáo trung thực khi nào rule là đủ.

## 7. Thống kê

- Nhiều incident hoặc truth thành phần trong một episode không độc lập → CI cho unsafe truth rate và các metric incident bằng **bootstrap theo episode**. Mỗi lần resample lấy nguyên episode với mọi truth/decision của nó rồi tính lại tỷ lệ; cùng chỉ số resample cho các policy khi so chênh lệch. Mẫu số 0 → `NA`, không báo 0%. Mẫu toàn 0 làm bootstrap suy biến, không phải bằng chứng không có rủi ro. Unsafe episode rate trong pilot là số đếm/tỷ lệ thực nghiệm; nhánh chứng nhận chính thức vẫn là phần mở rộng bên dưới.
- ECE nhiều bin không đáng tin với pilot nhỏ; ưu tiên risk–coverage và bảng số đếm. Không train thêm model confidence nhiều tham số từ vài chục incident.
- Với pilot ~16 episode có sự cố trong test, so sánh theo từng cơ chế gần như không có ý nghĩa thống kê; báo như pilot.
- *Mở rộng — nhánh chứng nhận:* `L_episode = 1` nếu có ít nhất một incident cần can thiệp bị đóng nhầm trong episode nguy hiểm. Policy cố định, episode mới độc lập cùng phân phối, exact binomial một phía 95%: 0 lỗi trên n cho cận trên `1 − 0,05^(1/n)` (n = 24 → 11,7%; n = 299 → ≤ 1%). Đây là rủi ro theo episode, không đổi tên thành theo incident, không chứng nhận thế giới thật. Chọn nhiều policy trên cùng tập phải dùng LTT hoặc tập chứng nhận mới; không đọc test nhiều lần tới khi CI đạt.

## 8. Xong khi

Có bảng và biểu đồ cuối cùng cho README: metric rủi ro trên tập truth/episode cố định, metric vận hành kèm số incident con, giới hạn quyền tự đóng của agent, mẫu số/độ bao phủ đánh giá và khoảng bất định phù hợp, kể cả kết quả âm tính. Không yêu cầu agent phải thắng baseline.

## 9. CLI chấm `decisions` và chọn cặp (P4.5)

- Lệnh: `python -m evaluation decisions --run <run-dir> [--policy-version V] [--model-slug S]`. Đọc `operational/observations.sqlite` ở chế độ read-only, ghi báo cáo vào `private/reports/p4_5_decisions.json`, in `policy_version` và `model_slug` đã chọn.
- **Một báo cáo chỉ gồm một cặp `(policy_version, model_slug)`.** Quyết định agent trong DB tạo danh sách cặp ứng viên. Nếu có nhiều hơn một cặp mà không truyền selector, lệnh từ chối: thoát mã 2, liệt kê các cặp, không ghi báo cáo.
- Selector phải chọn **đúng một** cặp. Không khớp cặp nào, hoặc khớp nhiều cặp (chỉ `--policy-version` khi policy đó có nhiều model), đều bị từ chối. Cặp agent chưa cấu hình (`model_slug` NULL) được chọn bằng `--policy-version` khi policy đó chỉ có một cặp.
- Quyết định `rule` (`model_slug` NULL) luôn được tính cho `policy_version` đã chọn. Quyết định của policy khác và quyết định agent của cặp khác không được tính; số bị loại ghi ở `selection.excluded_decision_count`.
- DB không có quyết định agent: cặp ứng viên là các `policy_version` của rule; có nhiều policy thì cần `--policy-version`.
- Báo cáo có khối `selection` gồm `policy_version`, `model_slug` (NULL khi cặp đã chọn là agent chưa cấu hình hoặc chỉ có rule) và `excluded_decision_count`. Tên file không đổi theo cặp, nên chạy cặp khác trên cùng run-dir sẽ ghi đè báo cáo trước; `selection` cho biết báo cáo thuộc cặp nào.
