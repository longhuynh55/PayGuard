# BACKLOG-7 — Review vòng 1 (Reviewer: Claude Opus 5.5)

Branch `slice/backlog-7` (commit `bcd85d3`), worktree `/home/lieu_hoai_phuc/.herdr/worktrees/Aiagent/slice-backlog-7`. Reviewer không sửa file nào trong repo/worktree; probe và output nằm ở `/tmp/claude-1000/backlog7-r1/`.

## Lệnh đã chạy
| Lệnh | Kết quả |
|---|---|
| `UV_CACHE_DIR=/tmp/payguard-uv-cache uv run pytest -q` (một lần, worktree) | 393 passed, 1 xfailed in 666.89s |
| `uv run python -m simulator generate --profile smoke --out /tmp/claude-1000/backlog7-r1/smoke` | 16 passed, 0 failed |
| `uv run python -m simulator validate --run /tmp/claude-1000/backlog7-r1/smoke` | 16 passed, 0 failed |
| `python3 review/check_links.py` | 16 files, 0 broken links |
| `python3 review/verify_plan_claims.py` | 19 examples; `git status` sạch, `verification_results.json` không đổi |
| `probe.py` (decisions) chạy trên worktree và trên main | `probe_new.txt` / `probe_main.txt` |
| `probe_recall.py` chạy trên worktree và trên main | xem F1 |

## Kiểm tra theo yêu cầu

### (1) Quy tắc `parent_scenario` (`evaluation/matcher.py:576-583, 746-758`)
Probe `/tmp/claude-1000/backlog7-r1/probe.py`. Mỗi fixture có 3 quyết định: close, human_review, fraud_ops. Kết quả branch so với main:

| Fixture | Status branch | Status main | Metric rủi ro/routing (unsafe ids/rate, timely, auto, human, dangerous) |
|---|---|---|---|
| 3 ứng viên cùng parent `p` (2 fraud + 1 benign) | `matched`, ids=[A,Bx,C] | `ambiguous_match` | giống hệt: unsafe=[A,Bx], timely=[A,Bx], dangerous 1/3 |
| cùng `p` + 1 truth NULL | `ambiguous_match` | `ambiguous_match` | giống hệt |
| cùng `p` + 1 truth parent `q` | `ambiguous_match` | `ambiguous_match` | giống hệt |
| cặp benign+fraud cùng `p` | `matched`, ids=[Bn,F] | `ambiguous_match` | giống hệt: unsafe=[F] |
| parent `""` cho cả hai | `EvaluationMatcherError: parent_scenario must be NULL or non-empty text` | `ambiguous_match` | (bị từ chối, đúng yêu cầu) |
| `"p"` và `" p"` | `ambiguous_match` | `ambiguous_match` | giống hệt |
| 1 ứng viên có parent | `matched` | `matched` | giống hệt |

- Quy tắc khớp đúng quyết định 31/10 ở mọi edge case.
- Detection recall dùng `candidate_truth_ids` (`evaluation/recall.py:40-44`), nên theo cấu trúc không đổi. Probe recall xác nhận detected=1/1 ở cả hai bản.
- Chỉ `decision_counts.matched/ambiguous/ambiguous_closures` và `unsafe_truth_rate.basis` đổi. Ví dụ basis đổi từ `all_candidate_truths_included` sang `matched_truth_only`. Đổi này hợp lý vì `matched_truth_ids` lúc này đã chứa mọi ứng viên, nên basis vẫn đúng nghĩa.
- `ambiguous_closures` không còn đếm closure cùng parent. Lý do: closure đó giờ là `matched`, và cảnh báo "cận dưới/ambiguous" không còn áp dụng.

### (2) Test thay đổi kỳ vọng
`git show main:tests/...` cho thấy cả 5 test bị lật đều dùng fixture cùng parent khác NULL ở main:
- `test_evaluation_decisions.py`: `mixed-scenario` (main:420/426), `delayed-mixed-scenario` (458/467), `two-cause-scenario` (490/495).
- `test_evaluation_recall.py`: `scenario-two-causes` (464/474, 527/537).

Ở mỗi test, assertion `status` được lật và một assertion `matched_truth_ids` được thêm vào. Không assertion nào khác bị xóa hay nới (diff chỉ chạm các dòng status/counts/ambiguous_incident_ids của chính các test đó). Test ambiguous thường vẫn còn:
- `test_ambiguous_close_scores_all_dangerous_candidate_truths`, parent NULL, có assert `ambiguous_closures == 1` và basis `all_candidate_truths_included`.
- Hai test param mới cho các ca khác parent, một NULL, cả hai NULL.

Test mới bao phủ các ca: cùng parent / khác parent / một NULL / cả hai NULL, cùng với unsafe/dangerous/timely/human_review giống nhau. Chuỗi trắng bị từ chối.

### (3) F2 (`tests/test_monitoring_metrics.py:769-776`)
- Diff chỉ đổi comment và thông điệp assert. `test_smoke_seasonal_volume_incidents_match_truth` (strict xfail) không bị chạm.
- Nội dung comment khớp bằng chứng trong P3.7-r2 F2: alert duy nhất là alert nền T0 ep-0003. Alert nền của ep-0004 thuộc episode không seasonal.

### (4) F4 (`monitoring/metrics.py:117-155, 267-272`)
- Test param bao phủ volume=2 → "must be 1 day", PSI=6 → "must equal window_days", và thiếu khóa volume → "must declare". Thông điệp lỗi rõ ràng.
- Config hiện tại vẫn nạp được.
- `git diff main...slice/backlog-7 -- configs` rỗng, tức không giá trị nào đổi.
- Monitoring và matcher giờ cùng ràng buộc PSI = `window_days`. Volume = 1 chỉ monitoring kiểm (xem F2 bên dưới).

### (5) Docs và smoke
- `docs/data.md:176,292,359`, `docs/monitoring.md:8` và `docs/p5-implementation.md:22-23` đều chỉ mô tả ngắn rồi link về `evaluation.md` mục 2. Không chép hợp đồng, không xóa giá trị khóa: dòng nghiệm thu (b) của P5.2 được giữ và chỉ thêm ghi chú ngoại lệ.
- Smoke: 4 truth và `parent_scenario` NULL ở cả 4. Validator cũng cấm non-NULL (`simulator/validate.py:642`). Vì vậy theo cấu trúc, status matcher trên smoke không thể đổi.
- Simulator và config không bị chạm, nên số alert/incident trên smoke giống main. Tôi không chạy lại pipeline smoke đầy đủ để so số đếm, vì đã chứng minh được bằng cấu trúc.

## Findings

### F1: [KHÔNG CHẶN MVP] `benign_matched` / `benign_incident_ids` giờ tính cả incident hai nguyên nhân có thành phần cần can thiệp
- **Vị trí:** `evaluation/recall.py:91-96` (`benign_incident_ids` = incident `matched` giao với truth benign); `docs/data.md:294`.
- **Phản ví dụ:** `/tmp/claude-1000/backlog7-r1/probe_recall.py` dùng một episode có F (fraud, cần can thiệp) và Bn (seasonal benign), cùng `parent_scenario="p"`, và một alert `amount_avg_7d`.
  - branch: `{"incident_counts": {"matched": 1, "ambiguous": 0, "benign_matched": 1}, "benign_incident_ids": ["incident-be1f…"]}`
  - main: `{"incident_counts": {"matched": 0, "ambiguous": 1, "benign_matched": 0}, "benign_incident_ids": []}`
- **Tác động:**
  - Đúng chữ định nghĩa hiện tại ("Incident khớp ít nhất một truth benign").
  - Nhưng một incident có fraud đồng thời giờ được đếm là "benign_matched". `docs/evaluation.md` mục 4 cấm thưởng benign khi có truth cần can thiệp đồng thời. Nếu P5.0b hoặc reward dùng lại bộ đếm này, nó sẽ bị phóng đại.
  - Hiện chưa có metric reward nào dùng bộ đếm này, và smoke không có parent khác NULL.
  - Chưa có test nào chốt hành vi này.
- **Đề xuất:** chọn một trong hai cách (orchestrator/người dùng chọn):
  - (a) Chỉ tính `benign_incident_ids` khi mọi `matched_truth_ids` đều benign.
  - (b) Giữ nguyên, nhưng ghi rõ trong `docs/data.md:294` rằng ca nhiều nguyên nhân có thành phần cần can thiệp vẫn được đếm, và thêm một test chốt hành vi.

### F2: [KHÔNG CHẶN MVP] Matcher vẫn không tự kiểm volume window = 1
- **Vị trí:** `evaluation/matcher.py:173` chỉ kiểm `psi.*` = `window_days`. Volume chỉ được kiểm trong `monitoring/metrics.py:146-150`.
- **Phản ví dụ (suy từ code):** nếu sửa `configs/monitoring.yaml` `volume.txn_count.value: 2`, job monitoring mới sẽ raise lỗi. Nhưng khi chấm lại một run đã có sẵn bằng `evaluate_detection_recall`/`evaluate_decisions`, `_public_metric_windows()` vẫn chấp nhận 2 và dùng cửa sổ 2+1 mà không báo lỗi.
- **Mức:** task chỉ yêu cầu validate phía monitoring, và đã đạt. Đây chỉ là lỗ hổng phòng thủ còn lại.
- **Đề xuất:** thêm vào matcher kiểm tra `volume_metric_id` = 1 với cùng thông điệp, hoặc ghi nhận là chấp nhận được.

### F3: [KHÔNG CHẶN MVP] Chặn parent chuỗi trắng chỉ nằm trong loader
- **Vị trí:** `evaluation/matcher.py:444-450` (loader) và `576-583` (`_shares_parent_scenario`).
- **Phản ví dụ:** gọi trực tiếp hàm public `match_incidents(...)` với truth_rows có `parent_scenario=""` ở cả hai: `_shares_parent_scenario({"A": "", "B": ""})` → `True` → `matched`.
- **Mức:** mọi đường chấm thật (`evaluate_detection_recall`, `evaluate_decisions`) đều đi qua loader và bị từ chối, như probe `blank_parent` cho thấy.
- **Đề xuất (tùy chọn):** coi chuỗi rỗng/trắng là không hợp lệ ngay trong `_shares_parent_scenario`, hoặc bỏ qua.

## Kết luận
- Cả 3 hạng mục được triển khai đúng phạm vi. Quy tắc `parent_scenario` đúng như người dùng chốt ở mọi edge case đã thử. Metric rủi ro/routing/detection giống hệt main trên cùng fixture.
- Test bị lật đều là fixture cùng parent, không nới assertion nào. Strict xfail seasonal còn nguyên. Config không đổi. Docs nhất quán và không chép hợp đồng.
- F1–F3 không chặn MVP. F1 nên được người dùng/orchestrator chốt trước P5.0b.

VERDICT: PASS
