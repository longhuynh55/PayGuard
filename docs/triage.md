# Triage — runbook, gate, agent và UQ

> Đầu vào: `incidents` ([monitoring.md](monitoring.md)), DB as-of và `business_calendar` ([data.md](data.md)) — **không** có truth. Đầu ra: bảng `decisions` cho evaluator.
> Stage xây: P4 (P4a rule · P4b `run_sql` · P4c agent).

## 1. Nguyên nhân, bằng chứng, hành động

| Nguyên nhân | Ý nghĩa | Routing ứng viên | Phạm vi rule |
|---|---|---|---|
| `seasonal` | Lễ, khuyến mãi, ngày lương | Đề xuất benign; tự đóng phải qua gate | known nếu khớp runbook lịch |
| `legit_business_change` | Thay đổi hợp lệ có đủ bằng chứng | Đề xuất benign qua gate | Theo runbook đã viết |
| `no_material_change` | Không có thay đổi trọng yếu, có đủ bằng chứng | Đề xuất benign qua gate | Theo runbook đã viết |
| `data_pipeline` | Đổi đơn vị, NULL hàng loạt | `data_ticket` | known |
| `fraud_surge` | Kiểu gian lận đã có trong train tăng đột biến | `fraud_ops` | known |
| `new_fraud` | Kiểu chưa từng thấy (social engineering) | `human_review` + đề xuất retrain | unknown |
| `other` | Chưa xác định trong taxonomy | `human_review` | — |

**Mẫu nhỏ/thiếu bằng chứng là `evidence_status = insufficient`, không phải nguyên nhân benign.** Mùa vụ trên lịch, p-value lớn hoặc ít dữ liệu không tự đủ để đóng.

```text
cause_hypotheses: tập giả thuyết + bằng chứng (không đồng nhất với nhiều nguyên nhân thật)
evidence_status: sufficient | insufficient | conflicting   ← do checklist tính, không do LLM
action: close | recheck | data_ticket | fraud_ops | human_review
close_score: score thực nghiệm, không phải xác suất đúng
as_of_time, policy_version, tool_trace_ids
```

## 2. Kiểm tra chung và quyết định theo hành động

`evidence_status` do code tính từ output của tool. Rule/agent đề xuất nguyên nhân, runbook và hành động; checklist chạy lại trên snapshot/phạm vi của incident. Mỗi hành động chỉ được đi qua nhánh tương ứng, không áp điều kiện benign cho routing.

1. **Kiểm tra chung:** snapshot/tool phải hợp lệ, đúng as-of/episode. Lỗi truy cập dữ liệu, bằng chứng mâu thuẫn hoặc điều kiện bắt buộc chuyển người trong runbook → `human_review`, không tự đóng hay routing chuyên biệt bằng bằng chứng đó. Chờ dữ liệu thiếu có thể dùng nhánh recheck dưới đây.
2. **Đề xuất `data_ticket` / `fraud_ops`:** yêu cầu bằng chứng routing do code kiểm tra. `data_ticket` cần checklist `unit_or_null_change` đạt và không có xung đột fraud. `fraud_ops` cần signal phía fraud đã được tool cấu trúc xác nhận trong đúng phạm vi và đủ cửa sổ quan sát; chỉ là chuyển nghi vấn để điều tra, không phải xác nhận fraud. Thiếu bằng chứng routing → `human_review`. Nhánh này **không** yêu cầu benign, `close_enabled` hay `tau_agent`.
3. **Đề xuất `close`:** cần checklist benign đạt, không điều kiện chuyển người nào bị kích hoạt, và có quyền đóng: rule → runbook đó `close_enabled`; agent → không `agent_close_disabled` và `close_score >= tau_agent` (mục 4). Không khớp runbook benign → `human_review`. Không đạt score/quyền đóng → recheck chỉ khi có thể bổ sung bằng chứng, nếu không thì chuyển người.
4. **Đề xuất `human_review`:** chuyển người, không yêu cầu benign hoặc score đóng. **`recheck`:** chỉ dùng khi ghi được lý do/bằng chứng còn chờ; tối đa **hai lần, cách nhau một ngày**, sau đó chuyển người nếu chưa đủ điều kiện. Case chờ vẫn tính workload và unresolved; khi có bằng chứng mới, chạy lại nhánh tương ứng.

Giới hạn recheck được tính trên các quyết định `action = 'recheck'` đọc từ bảng operational `decisions` qua `read_as_of` với đúng `episode_id` và `available_at <= as_of_time` hiện tại. Một quyết định đã được ghi với cutoff hiện tại cũng tính trong lịch sử nhìn thấy tại cutoff đó. Mỗi `incident_id` có tối đa hai quyết định recheck; incident con có `parent_incident_id` bắt đầu bộ đếm riêng, không cộng recheck của cha hoặc các incident khác trong lineage. Hai lần recheck liên tiếp phải cách nhau ít nhất **24 giờ đã trôi qua**, so bằng timestamp UTC đã chuẩn hóa của hai `as_of_time` (lần sau trừ lần trước `>= 24 giờ`, không chỉ so ngày lịch). Nếu đề xuất recheck thứ ba hoặc đề xuất mới đến trước mốc 24 giờ, gate trả `human_review` và ghi lý do giới hạn vào `reason`. Quy tắc áp dụng giống nhau cho nguồn `rule` và `agent`, kể cả khi gate chuyển một đề xuất `close` thành recheck; `evidence_status` vẫn do code tính.

Điều kiện bắt buộc chuyển người có ưu tiên cao hơn các nhánh khác. Singleton nguy hiểm vẫn routing; chỉ nhánh `close` dùng gate tự đóng.

## 3. Checklist runbook (MVP: 4 runbook, viết bằng code)

"Bắt buộc" sai → `insufficient`; "chuyển người" đúng → `human_review` bất kể score.

| Runbook | Kiểm tra bắt buộc | Cửa sổ phải hoàn tất | Bắt buộc chuyển người khi |
|---|---|---|---|
| `seasonal_calendar` → benign | Ngày ảnh hưởng trong `business_calendar` giao với cửa sổ alert; (a) **vị trí:** độ lệch theo ngày tập trung vào ngày ảnh hưởng, các ngày còn lại trong dải tham chiếu; (b) **độ lớn:** khớp kỳ vọng tổng hợp từ nền từng ngày theo công thức bên dưới; (c) hướng và segment khớp `expected_effect` / `scope` | Cửa sổ 7 ngày đã đóng tại `as_of_time` | Bất kỳ metric phía fraud nào (chuyển ra 24 h, người gửi lần đầu, số tiền sát 10 triệu, tỉ lệ review của model) có signal trong cùng segment |
| `unit_or_null_change` → `data_ticket` | Tỉ số trung vị sau ÷ trước nằm trong dải log ×/÷2 quanh {100, 1/100, 1000} (tức `[target/2, target×2]`; bản đầu ± 5%, đổi 27/10), hoặc tỉ lệ NULL tăng ≥ 20 điểm %; thay đổi đột ngột trong 1 ngày; mọi ngày pre/post của transition candidate có ít nhất n_min giao dịch trong segment và channel đang so | Ít nhất 1 ngày sau điểm đổi | Cùng lúc có signal fraud trong segment |
| `small_sample` → `insufficient` | Ngày pre hoặc post của transition candidate có số giao dịch trong segment và channel đang so < n_min | — | Hết 2 lần recheck vẫn < n_min |
| `segment_concentration` → chỉ khoanh vùng | Một segment chiếm ≥ 70% đóng góp PSI | Cửa sổ đã đóng | Không tự quyết định; chỉ thu hẹp phạm vi cho runbook khác/agent |

- Không dùng ngưỡng độ phủ cửa sổ cho mùa vụ: sự kiện 1 ngày chỉ phủ tối đa 1/7 ≈ 14,3% cửa sổ 7 ngày, nên "phủ ≥ 50%" loại mọi ngày lương, 11/11, khuyến mãi một ngày. Kiểm tra vị trí theo ngày còn bắt được fraud chen giữa đợt mùa vụ.
- **Kỳ vọng cửa sổ có trọng số:** hợp đồng `expected_effect` do [data.md](data.md) sở hữu. Với count/tổng tiền, nền ngày d là B_d và thay đổi tương đối r_d (0 ngoài ngày ảnh hưởng): kỳ vọng tổng = `sum(B_d * (1 + r_d))`. Relative lift = `sum(B_d * r_d) / sum(B_d)` khi tổng nền > 0; không chia đều cho 7 nếu các ngày có nền khác nhau.
- Với tỷ lệ/trung bình, tính riêng tử dự kiến `sum(N_d * (1 + a_d))` và mẫu dự kiến `sum(D_d * (1 + b_d))`, rồi mới chia. Không trung bình đều các tỷ lệ theo ngày. Mẫu số <= 0, thiếu nền hoặc thiếu khai báo metric → `insufficient`. So sánh observed với expected bằng tolerance chốt trên development: `abs_tol + rel_tol * abs(expected - baseline)` trên đơn vị metric; nền bằng 0 không tính relative lift, chỉ so giá trị tuyệt đối với tolerance đã khai báo. PSI/KS không dùng trực tiếp các công thức cộng này.
- Ví dụ nền `[1000,10,10,10,10,10,10]`, ngày đầu tăng 40%: expected lift tuần = 400/1060 = 37,74%, không phải 40%/7 = 5,71%. Các dải theo ngày không được fit lại bằng test. Checklist không bảo đảm bắt được fraud trùng đúng ngày ảnh hưởng; vẫn cần các điều kiện chuyển người phía fraud và đánh giá lỗi trên held-out.
- **Dải ×/÷2 cho tỉ số trung vị (người dùng chốt 27/10):** lỗi đơn vị luôn là lũy thừa của 10, còn dao động trung vị tự nhiên giữa các ngày không quá ~2 lần. Với ~9 giao dịch/kênh/ngày như smoke, trung vị ngày của episode clean đã lệch tới ~2 lần, nên ± 5% không thể đạt kể cả khi đổi cửa sổ. Dải này suy ra từ cấu trúc lỗi, không fit trên episode.
- **`n_min = 2` — khởi đầu đã khóa theo BACKLOG-5 round 1:** trên manifest smoke, chỉ dùng operational data của episode `ep-0001` (`development`), đếm giao dịch theo ngày trong segment `all`, tách theo channel như runbook đang so. Lần chọn đầu trên world trước P1.3 vòng 2: `qr` 237 ngày có giao dịch, median 3/ngày, 30 ngày singleton; `card` 37 ngày, ≥ 8/ngày. Đo lại sau khi P1.3 vòng 2 merge (review BACKLOG-5 r1, 31/10): `card` 138 ngày, median 1,5/ngày, 69 singleton; `qr` 196 ngày, median 2, 93 singleton; `wallet` 184 ngày, median 2, 71 singleton — n_min chặn khoảng một nửa số ngày-kênh, nên always-recheck tăng 30 → 58 lời gọi. Cả hai world đều cho số nguyên nhỏ nhất loại mẫu đơn: `n_min = 2`; vì vậy một transition median/NULL chỉ được pass khi cả ngày pre và post đều có ≥ 2 giao dịch. Không dùng calibration hay test để chọn giá trị. Đây là ngưỡng khởi đầu, không phải bảo đảm đủ mẫu thống kê; muốn đổi phải chọn lại trên development rồi khóa trong policy version mới.
- Mọi ngưỡng khác (dải tham chiếu theo ngày, sai số độ lớn, dải ×/÷2 của tỉ số trung vị, 20 điểm, 70%) là giá trị khởi đầu, chỉnh trên development rồi khóa.
- Rule chỉ viết cho kịch bản *đã biết*. Alert không khớp rule nào, khớp nhiều rule mâu thuẫn, hoặc khớp runbook đang tắt quyền đóng → chuyển cho agent (kết luận của rule làm ngữ cảnh).

## 4. Quyền tự đóng: rule và agent chọn riêng

Nếu dùng chung một tau, rule có score cố định 1 nên một runbook đóng nhầm sẽ loại mọi tau và tắt cả hệ thống, kể cả khi agent phân biệt đúng (phản ví dụ `separated_rule_agent_authority` trong `review/`). Chọn hai bước, lưới khai báo trước, trên tập calibration:

1. **Rule:** mỗi runbook benign `close_enabled ∈ {on, off}`. On khi trên calibration không có unsafe closure do runbook đó và có ít nhất một sự cố benign truth được đóng đúng.
2. **Agent:** với cấu hình rule đã khóa, chọn `tau_agent ∈ {2/3, 1}` trên phần ca thực sự tới agent: tối đa số sự cố benign truth được đóng đúng (đếm ID duy nhất theo [evaluation.md](evaluation.md)) với không unsafe closure quan sát; hòa lấy tau lớn hơn; không có tau hữu ích → `agent_close_disabled` (agent vẫn routing). Incident con lặp không làm tăng mục tiêu tuning; ca chưa rõ đáp án không được coi là benign.

Cả hai bước là tuning thực nghiệm trên vài chục ca, không phải bảo đảm rủi ro. Mọi sửa runbook/prompt/sampling/model sau đó tạo policy version mới. Lỗi tự đóng báo riêng theo rule/agent và theo từng runbook.

## 5. Agent điều tra

- Vòng ReAct, kích hoạt bởi incident rule không xử lý được, hoặc câu hỏi tiếng Việt của người vận hành. Kết quả rule làm ngữ cảnh ban đầu.
- **Giới hạn vòng 1 P4.4b:** mỗi incident được agent nhận chạy đúng một `run_sql` trên snapshot operational as-of, sau đó đúng một lần sinh JSON gồm `cause`, `action`, `reason` (có thể kèm `runbook`); chưa dùng UQ. `agent_close_disabled = true`, agent không bao giờ được gate cho phép `close` (kết quả `human_review` hoặc `recheck` theo mục 2). Lỗi tool, proposal rỗng/sai format hoặc hết ngân sách cũng đi qua gate thành `human_review`. Nếu lần sinh SQL gặp lỗi LLM (lỗi API, JSON sai, nội dung rỗng, hết token, hết ngân sách) thì agent bỏ lần sinh proposal và dùng proposal dự phòng `cause = other`; kết quả vẫn qua gate thành `human_review`, usage chỉ tính một lời gọi. Trace ID của `run_sql`, usage gộp hai lần gọi và canonical model slug được ghi trong `decisions`.
- **Tools** (đều đúng as-of, không chạm truth): `run_sql(question)`, `drift_report(date_range)`, `segment_breakdown(metric, dims)`, `explain_txn(txn_id)` (SHAP).
- **Score MVP:** K = 3 lượt điều tra, mỗi SQL N = 3 mẫu; `close_score = min(v_benign, min_sql_consistency)` trên các SQL liên quan; `v_benign` = số lượt kết luận benign / 3. Lượt không hoàn tất là phiếu không hỗ trợ benign; SQL lỗi consistency 0. Không dùng SQL thì bỏ thành phần SQL nhưng vẫn cần bằng chứng từ tool cấu trúc. Không nhân các confidence để coi là xác suất toàn chuỗi.
- **Kết luận ứng viên:** nguyên nhân chiếm đa số trong 3 lượt; không có đa số → không phải benign. Majority vote là cách tổng hợp dự đoán, không phải gate.
- Hành vi bắt buộc cho: thiếu bằng chứng, tool lỗi, hết ngân sách, recheck.
- **Giới hạn MVP:** chỉ `seasonal_calendar` hiện là runbook benign cho phép đóng. Khi rule và agent kiểm tra cùng snapshot/phạm vi và runbook đang bật, ca đạt checklist đã được rule đóng trước; agent không tạo thêm quyền đóng cho nguyên nhân ngoài runbook. Khi runbook bị tắt, agent có thể lọc lại các ca trong phạm vi đó bằng bằng chứng điều tra, nhưng vẫn phải đạt checklist. `legit_business_change` / `no_material_change` không khớp runbook benign hiện có → chuyển người, dù chẩn đoán của agent có thể đúng.
- Giá trị cần đo của agent là chẩn đoán có bằng chứng, routing và kết quả trên phần ca thực sự được chuyển tới; tự đóng là phần bổ sung trong phạm vi được phép. Không trình bày kết quả như khả năng tự xử lý mọi benign chưa biết. Giữ nguyên 4 runbook MVP; thêm runbook benign là thay đổi scope/policy cần chốt riêng. Cách báo cáo từng nhóm: [evaluation.md](evaluation.md).
- *Mở rộng:* adaptive N / thay K (phải đánh giá lại nguyên policy); conformal agent kiểu KnowNo — tập rỗng hoặc đa nhãn chuyển người, không dùng coverage làm chứng nhận lỗi tự đóng; LTT.

### Tool `run_sql` có UQ
- Sinh N = 3 SQL cố định, chạy, gom cụm theo kết quả; execution consistency là score.
- **Cache key:** question/prompt, model/version, sampling config, schema, episode, `as_of_time`, data snapshot. Replay cache không tính là mẫu độc lập mới.
- **Bộ câu hỏi:** 100–150 câu JSON (`question`, `category`, `interpretations`, `expected_behavior` ∈ answer/clarify/abstain); tỉ lệ rõ ràng 40 · mơ hồ 30 · không trả lời được 15 · bẫy thống kê 15. Xây: template → LLM đã chốt ([rationale.md](rationale.md) mục 3) paraphrase kiểu người Việt hỏi → tự review.
- **Chấm:** kết quả trên nhiều DB phản ví dụ (sai mẫu số/join, duplicate, NULL, ngày biên, pending label), tolerance ghi rõ theo metric; clarify/abstain chấm riêng. SQL cùng kết quả trên một DB chưa chứng minh đúng nghĩa. MVP: vài DB phản ví dụ; *mở rộng:* bộ đầy đủ, user simulator.
- Baseline confidence: verbalized confidence · logprob (nếu API có) · self-consistency theo chuỗi SQL.

## 6. Bảng `decisions`

Mỗi lần gate chạy (kể cả recheck) ghi một dòng: `decision_id`, `incident_id`, `decision_at`, `as_of_time`, source (rule/agent), runbook, action, phạm vi (feature/segment/khoảng thời gian), kết quả từng mục checklist, `tool_trace_ids`, kết luận và lý do, `close_score`, `policy_version`, canonical `model_slug`, token/chi phí.

## 7. Xong khi

Có kiểm tra cho đóng benign, routing pipeline không cần benign/score, routing fraud có bằng chứng, xung đột bắt buộc chuyển người, thiếu bằng chứng/tool lỗi/hết ngân sách/recheck; chạy hết episode trong trần chi phí. Báo lỗi rule/agent và các ca ngoài runbook benign riêng.

## 8. Trace `run_sql` P4.4a

Mỗi lần gọi `run_sql` trả `tool_trace_id` tất định và trace tạm thời gồm SQL đã sinh (NULL nếu chưa có), số dòng trả về, cờ cắt ở giới hạn dòng, fingerprint snapshot as-of, lỗi có cấu trúc hoặc NULL, và record LLM. Record LLM có model trả về, canonical slug, provider, prompt/completion/reasoning tokens, chi phí USD, độ trễ và cache key theo mục 5. ID trace được dẫn xuất từ câu hỏi, episode, `as_of_time`, fingerprint snapshot và cache key; cùng đầu vào tạo cùng ID. Trace không chứa API key, đường dẫn DB hoặc dữ liệu từ bảng private.

**Snapshot và cache (PERF-TRIAGE, 02/11/2026).** `run_sql` dựng snapshot từ `read_as_of` cho mọi bảng as-of và giữ nguyên declared type của từng cột theo `PRAGMA table_info`; SQLite áp type affinity khi so sánh, nên cùng câu SQL trả cùng kết quả như trên bảng gốc as-of (ví dụ `amount = '125000'` khớp cột `INTEGER`). Hash `data_snapshot` và cô lập SQL (chỉ đọc, chỉ schema `asof`, timeout, giới hạn dòng/byte) không đổi. Agent dùng một `SnapshotCache` chung: snapshot của cùng `(episode, as_of_time)` được tái dùng khi `PRAGMA data_version` của DB không đổi; mọi commit khác kết nối làm dựng lại, kể cả quyết định ghi tại chính cutoff đó (quyết định hiển thị tại cutoff của nó). Runner đọc runbook một lần cho mỗi block `(episode, as_of_time)` và chia sẻ cho mọi incident trong block. Block ghi `incidents` trước vòng lặp, rồi trong vòng lặp chỉ ghi `decisions`; runbook không đọc `decisions`, nên các dòng runbook đọc không đổi trong block.

Lỗi dựng snapshot có mã phân loại thay cho `snapshot_error` cũ: `invalid_as_of`, `snapshot_source_unavailable`, `snapshot_scope_invalid`, `snapshot_schema_unsupported`, `snapshot_value_unsupported`, `snapshot_internal_error`; lỗi bất ngờ ngoài pha snapshot và LLM/SQL là `tool_internal_error`. Thông báo trả cho agent không chứa đường dẫn hay nội dung ngoại lệ; log chẩn đoán chỉ ghi giai đoạn và tên lớp ngoại lệ.

## 9. CLI trong P5.1 harness

Trong `<benchmark-root>/policies/<policy>`, `python -m triage run` và `python -m triage measure` kiểm tra hash nội dung runtime/config/prompt/sampling với harness và policy manifests trước khi mở operational DB hoặc ghi output; thay đổi Git HEAD/dirty flag không chặn resume. Harness triage cũng xác nhận BASE đang đóng băng, fingerprint policy copy bằng BASE, và decision rows hiện có khớp `policy_version`/`model_slug` trong manifest. Held-out role chỉ được triage sau khi lock tương ứng tồn tại và khớp run manifest.

Lệnh standalone `triage run` dùng config mặc định đã được identity bao phủ khi chọn calibration/test; custom `--simulator-config`, `--triage-config` hoặc `--llm-config` path bị từ chối trước khi dựng provider. `triage measure` luôn dùng config mặc định đã được identity bao phủ vì có thể chuyển từ development sang calibration; custom path bị từ chối trước khi provider hoặc report được tạo.

`triage run` mặc định chỉ xử lý development. Có thể lặp `--episode-role development|calibration|test` để chọn role; chỉ episode ID của role được chọn đi vào runner. Lệnh standalone cũng áp role lock qua state directory chung; OpenRouter standalone dùng ledger bền theo [data.md §9](data.md#9-p51-harness-artifacts). Provider và model phải khớp policy identity.

Harness truyền durable ledger chung ở `<state-dir>/cost-ledger.jsonl` cho mọi provider có thể gọi LLM; role ledger và lock markers cũng cùng thư mục state ngoài các benchmark roots (xem [data.md §9](data.md#9-p51-harness-artifacts)). Cap $10 USD và nguồn giá trị được ghi ở [p5-implementation.md](p5-implementation.md); mode P4 ngoài harness giữ cap $1 trong `configs/llm.yaml`. `measure` chọn development trước, chỉ đọc calibration khi chưa đủ mẫu development và khi callback lock guard cho phép; role đã chạm được ghi vào ledger. CLI evaluator, role locks, report paths và hành vi decision set rỗng được định nghĩa tại [evaluation.md, mục 9](evaluation.md#9-cli-cham-decisions-va-chon-cap-p45-p51).

Harness root hiện không có process lock. Chạy tuần tự các policy/lệnh trên cùng root; không khởi chạy đồng thời nhiều harness hoặc triage process trên một root.
