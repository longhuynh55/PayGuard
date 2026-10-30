# PayGuard — Master Plan

> Đích đến, trạng thái, lộ trình theo stage, phạm vi MVP và nhật ký quyết định. Cập nhật: 30/10/2026.
> Thiết kế chi tiết nằm trong `docs/` theo module; **mỗi nội dung chỉ viết ở một chỗ**, file khác link tới.

| Tài liệu | Nội dung |
|---|---|
| [docs/overview.md](docs/overview.md) | Business problem, giải pháp, ràng buộc, kiến trúc, rủi ro, cách trình bày |
| [docs/data.md](docs/data.md) | Simulator, schema as-of, nhãn, `business_calendar`, episode, bảng truth — **sở hữu mọi schema dữ liệu** |
| [docs/p1-implementation.md](docs/p1-implementation.md) | Thứ tự task, file dự kiến, kiểm tra và nghiệm thu implementation P1; schema vẫn ở `docs/data.md` |
| [docs/orchestration.md](docs/orchestration.md) | Goal hiện tại, vòng lặp tự điều phối, ranh giới tự quyết, nhật ký chạy |
| [docs/model.md](docs/model.md) | Split 3 tập, LightGBM, calibration, conformal giao dịch |
| [docs/monitoring.md](docs/monitoring.md) | Metric → signal → alert → incident, incident con |
| [docs/triage.md](docs/triage.md) | Taxonomy, runbook checklist, gate, quyền đóng, agent, `run_sql` |
| [docs/evaluation.md](docs/evaluation.md) | Vai trò episode, ghép truth, metric, baseline, thống kê |
| [docs/app.md](docs/app.md) | API, UI, Docker, demo |
| [docs/rationale.md](docs/rationale.md) | Nghiên cứu, lý do thiết kế, tài liệu tham khảo |
| [archive/](archive/) | Bản plan cũ và lịch sử review — không cập nhật nữa |

---

## 1. Đích đến

**Business problem:** alert drift của model fraud quá nhiều, phần lớn vô hại; nhãn về chậm 30–90 ngày; điều tra mỗi alert tốn hàng giờ → alert fatigue.

**Project "xong" khi có đủ:**
1. Luồng end-to-end chạy bằng một lệnh: dữ liệu → model → monitoring → alert → rule triage → agent → báo cáo.
2. Bảng kết quả trả lời được: *hybrid rule + agent có UQ có hơn rule-based một mình không, ở nhóm sự cố nào, với chi phí bao nhiêu?*
3. Demo: video 3 phút + `docker compose up` + Streamlit online chỉ đọc kết quả.
4. README có problem statement, kiến trúc, kết quả, design decisions, limitations, related work.
5. Nhãn/feature đúng as-of; evaluator tính cả monitoring bỏ sót và rule đóng nhầm; mọi tỷ lệ lỗi có mẫu số và khoảng bất định phù hợp. Pilot không được diễn giải thành bảo đảm rủi ro gần 0.

---

## 2. Trạng thái

| Stage | Module | Trạng thái |
|---|---|---|
| P0 | Research, thiết kế | ✅ Protocol cập nhật 25/10/2026 sau sửa rule/agent và đơn vị đánh giá; trạng thái runtime xem tại P1–P6 bên dưới |
| P1 | Simulator + injector ([data](docs/data.md)) · [sub-plan](docs/p1-implementation.md) | 🔁 Vòng 1 hoàn tất ✅ (P1.1–P1.8). Vòng 2: **P1.3 merge 31/10** (kênh QR/ví/thẻ, vai trò tài khoản private, A2A, refund/reversal, sửa `_poisson_count`; review r2 PASS; 321 passed + 1 strict xfail ca FP clean `ep-0001` chờ BACKLOG-5); P1.4, P1.6, P1.7, P1.8 vòng 2 chưa làm, sau các task chuẩn bị P5; bản 24/10 đã rollback |
| P2 | Fraud model ([model](docs/model.md)) · [sub-plan](docs/p2-implementation.md) | ✅ P2 vòng 1 hoàn tất: P2.1 audit split, P2.2 feature as-of, P2.3 LightGBM + calibration, P2.4 Mondrian conformal trên score thô, P2.5 `model_scores` + smoke evaluation đều đã bàn giao; bản 24/10 đã rollback |
| P3 | Monitoring ([monitoring](docs/monitoring.md)) · [sub-plan](docs/p3-implementation.md) | ✅ P3 vòng 1 hoàn tất: P3.1–P3.4 và P3.5 `round1-done` (Vòng 1 đã bàn giao; chi tiết P3.4 và P3.5 tại [sub-plan](docs/p3-implementation.md)); phần vòng 2 chưa triển khai; BACKLOG-2 (28/10): `model score`/`model evaluate` không còn nạp `configs/episodes.yaml` |
| P4 | Triage ([triage](docs/triage.md)) · [sub-plan](docs/p4-implementation.md) | 🔁 Sub-plan đã duyệt 26/10; P4.1 vòng 1 ✅ và vòng 2 phần recheck cap xong 30/10 (xung đột nhiều rule còn hoãn); P4.2–P4.5 vòng 1 hoàn tất; P4.4b hoàn tất sau review r2 và fail-fast fix r3 (mỗi run directory dùng một agent provider); P4.6 round 1 hoàn tất với 2 incident theo quyết định người dùng 29/10; ca thứ ba hoãn tới pilot 60 P1 vòng 2 ([chi tiết](docs/p4-implementation.md)); BACKLOG-4 (29/10) sửa backlog P4.6; P3.6 tách cấu hình injector khỏi monitoring |
| P5 | Đánh giá ([evaluation](docs/evaluation.md)) · [sub-plan](docs/p5-implementation.md) | 🔁 30/10: chuẩn bị P5 (bảng quyết định UQ, matcher, support development, harness + sổ chi phí) chen trước P1.4 |
| P6 | Đóng gói ([app](docs/app.md)) | ⬜ |

**Việc tiếp theo:** xong đợt 1 P1 vòng 2 (P1.3) → các task chuẩn bị P5 theo [sub-plan P5](docs/p5-implementation.md) → P1.4 trở đi theo [sub-plan](docs/p1-implementation.md); ca P4.6 thứ ba hoãn tới pilot này theo quyết định người dùng 29/10 (mục 5). P3.5 vòng 1 đã lưu `daily_metrics`, `alerts` và `incidents` vào DB operational, có allowlist as-of; P4 đọc snapshot qua `read_as_of`. Mốc ★ đạt 29/10; ngân sách benchmark đã khóa (mục 5: 10 USD, 60 episode). Không thêm vòng review protocol trước khi có luồng mỏng chạy được. [19 ví dụ kiểm tra protocol](review/verify_plan_claims.py) kiểm tra phép tính, phân nhánh hành động, đơn vị rủi ro và cửa sổ gộp incident; chúng không kiểm tra implementation.

---

## 3. Lộ trình (~7 tuần)

Ước lượng theo ngày làm việc. ★ = mốc end-to-end bản thô. Chỉ làm phần MVP (mục 4).

| Stage | Việc | Xong khi |
|---|---|---|
| **P1** Simulator + injector (7 ngày) | Thế giới nền, schema as-of, label events + tail, `business_calendar`; injector các kịch bản; bảng truth có `effect_available_at` / `accepted_actions` / `intervention_start_at` / `deadline_at`; 60 episode | Integrity + leakage check đạt; truy vấn tại t không đổi khi thêm dữ liệu sau t; snapshot nhãn đúng 4 trạng thái; mỗi episode có truth đúng; truth không truy cập được từ DB của agent |
| **P2** Model (5 ngày) | Snapshot nhãn, split 3 tập, feature as-of, LightGBM, isotonic, conformal trên score thô | Xem [model.md](docs/model.md) mục Xong khi |
| **P3** Monitoring (4 ngày) | Job hằng ngày, signal, alert → incident, incident con; evaluator đối soát detection recall | Mỗi episode sinh alert tái lập được |
| ★ Luồng mỏng (cuối tuần 3) | snapshot → score → một monitor → một rule → gate → `decisions` → evaluator; thêm agent một bước (1 SQL, không UQ); đo token trên 3 case | Ra bảng kết quả dù còn xấu; có số token thật |
| **P4** Triage (10 ngày) | P4a 4 runbook checklist theo metric + quyền đóng theo runbook (2 ngày) · P4b `run_sql` N = 3 + bộ câu hỏi + DB phản ví dụ (4 ngày) · P4c agent K = 3, phân nhánh đóng/routing/recheck (4 ngày) | Xem [triage.md](docs/triage.md) mục Xong khi |
| **P5** Đánh giá (4 ngày) | 4 baseline + hai phép so sánh agent; utility + độ nhạy; ablation có công khai chi phí | Bảng và biểu đồ cuối cho README, kể cả kết quả âm tính |
| **P6** Đóng gói (5 ngày) | FastAPI, Streamlit, Docker Compose, pytest + CI, README, video | `docker compose up` chạy được demo |

Nâng cấp sau (không chặn): Kafka/Spark/Airflow · ensemble epistemic · user simulator · Next.js · user study nhỏ.

---

## 4. Phạm vi MVP và phần làm sau

MVP là phần tối thiểu để trả lời câu hỏi chính. Phần "làm sau" không sai, chỉ chưa làm thay đổi kết luận chính; schema chừa sẵn cột để thêm sau không phải viết lại. Mỗi module cũng gắn nhãn *Mở rộng* tại chỗ.

| Nhóm | MVP | Chi tiết |
|---|---|---|
| Nhãn | 4 trạng thái, H = 90, một kết luận/giao dịch | [data](docs/data.md) |
| As-of | `event_at`/`available_at`; mọi feature/SQL giới hạn theo `as_of_time` và episode | [data](docs/data.md) |
| Truth | File/DB riêng, tool agent không kết nối tới | [data](docs/data.md) |
| Split | Model 3 tập; episode dev/calibration/test + manifest khóa trước tuning | [model](docs/model.md), [evaluation](docs/evaluation.md) |
| Quyết định | cause / evidence_status / action tách riêng; 4 runbook bằng code; đóng và routing qua nhánh riêng; quyền đóng rule/agent chọn riêng; recheck ≤ 2 | [triage](docs/triage.md) |
| Incident theo thời gian | Gộp vào incident đang mở; đã quyết định thì tạo incident con; chấm theo `decision_at` | [monitoring](docs/monitoring.md), [evaluation](docs/evaluation.md) |
| UQ | Agent K = N = 3, tau ∈ {2/3, 1}; giao dịch Mondrian conformal | [triage](docs/triage.md), [model](docs/model.md) |
| Đánh giá | Rủi ro so policy trên tập truth/episode cố định; detection, timely routing, metric incident/workload/chi phí; báo giới hạn quyền đóng agent; bootstrap theo episode | [evaluation](docs/evaluation.md) |
| Baseline | 4 baseline + hai phép so sánh agent | [evaluation](docs/evaluation.md) |
| SQL | Bộ câu hỏi 40/30/15/15 + vài DB phản ví dụ | [triage](docs/triage.md) |

**Làm sau:** `disputed` và sửa nhãn, H theo kênh, H = 30/60 · oracle-label ablation, feedback sớm vào train · conformal agent (KnowNo), adaptive N / thay K, LTT · nhánh chứng nhận 299 episode, stress test cơ chế mới · phân xử thủ công ca ambiguous, bộ DB phản ví dụ đầy đủ · MLflow.

---

## 5. Ngân sách LLM ✅ — khóa 29/10/2026 sau luồng mỏng

Simulator sinh dữ liệu gần như miễn phí; giới hạn thật của số episode là chi phí agent. 60 episode là **sàn**. Cần pipeline để đo nên chia hai mức: trần tạm cho pilot (không chặn P1–P3) và ngân sách benchmark khóa sau khi đo.

- Ước lượng thô mỗi incident tới agent: 3 lượt × ~5 SQL × 3 mẫu ≈ 45 lần sinh SQL + ~15–20 bước suy luận ≈ **60 lần gọi**. Chi phí ≈ số incident tới agent × 60 × token/lần × giá. So sánh "giá trị vận hành" chạy thêm một lượt K = N = 1 trên mọi ca tới agent.
- **Việc:** (1) LLM đã chốt Qwen 3.7 Flash qua OpenRouter (giá và lý do: [rationale.md](docs/rationale.md) mục 3), đặt trần tạm; (2) ở mốc ★, chạy 3 incident cố định đo token thật; (3) khóa trần USD cho toàn benchmark (gồm 4 baseline và ablation) → suy ra số episode; nếu vượt 60 thì tăng calibration/test theo tỉ lệ 1:2:2.
  - **P4.6, quyết định người dùng 29/10/2026:** lượt này đo 2 incident agent-eligible thay vì 3 (`ep-0001` development, `ep-0003` calibration; cả hai tại `2026-02-07T16:59:59.999999Z`). Không chọn test episode; ca thứ ba hoãn tới pilot 60 ở P1 vòng 2. Bốn lời gọi thật trên `qwen/qwen3.7-flash-20260727`:

    | Lời gọi | Incident | Token vào | Token ra | Tổng token | Chi phí USD | Độ trễ ms |
    |---|---|---:|---:|---:|---:|---:|
    | sinh SQL `run_sql` | ep-0001 / incident-c89881b3dc8c2d6e9ebd1354 | 3,229 | 257 | 3,486 | 0.0001302800 | 3,519.425 |
    | proposal | ep-0001 / incident-c89881b3dc8c2d6e9ebd1354 | 3,424 | 56 | 3,480 | 0.0001100000 | 1,250.899 |
    | sinh SQL `run_sql` | ep-0003 / incident-fff68272d2477ac4092c8e68 | 3,224 | 194 | 3,418 | 0.0001219400 | 2,233.807 |
    | proposal | ep-0003 / incident-fff68272d2477ac4092c8e68 | 3,340 | 135 | 3,475 | 0.0001354125 | 3,501.848 |
    | Trung bình mỗi lời gọi | — | 3,304.25 | 160.5 | 3,464.75 | 0.000124408125 | 2,626.495 |
    | Tổng (4 lời gọi) | — | 13,217 | 642 | 13,859 | 0.0004976325 | 10,505.980 |

    Bối cảnh: ở `ep-0001`, SQL do LLM sinh chạy lỗi (`sql_execution_error`); cả hai decision đều ra `human_review` với `evidence_status = insufficient`, nên token proposal của `ep-0001` chưa phản ánh ca SQL thành công.

    Chỉ là ngoại suy: với khoảng 60 lời gọi mỗi incident tới agent, chi phí trung bình đo được cho **khoảng 0,0075 USD (0,0074644875) mỗi incident tới agent**. Đây là ước lượng cho thiết kế đầy đủ, không phải kết quả đo. Trần USD benchmark và số episode chưa khóa, chờ người dùng quyết sau khi xem số đo.
  - **Khóa ngân sách — người dùng chốt 29/10/2026:** trần **10 USD** cho toàn benchmark (gồm 4 baseline và ablation); số episode giữ **60** (pilot 12/24/24). Sau pilot 60, đo số incident tới agent thật mỗi episode; chỉ tăng episode (calibration/test theo tỉ lệ 1:2:2) nếu người dùng duyệt lại. Trần tạm 1 USD trong `configs/llm.yaml` vẫn áp cho P4 vòng 1; đổi sang trần benchmark khi triển khai chạy benchmark.

## 6. Tái lập

`git init` ngay khi bắt đầu code; manifest seed/split/config/model; dữ liệu sinh lại và môi trường ảo không đưa vào git. Không giả định số đo cũ đã được tái lập.

---

## 7. Nhật ký quyết định

| Ngày | Quyết định |
|---|---|
| 30/10/2026 | Sau phản biện kiến trúc (orchestrator + supervisor), người dùng duyệt chen trước P1.4: bảng quyết định UQ, matcher hoàn chỉnh, bảng support development, harness + sổ chi phí bền ([sub-plan P5](docs/p5-implementation.md)); P4 vòng 2 vẫn sau P1. Chỉ orchestrator cập nhật Trạng thái MASTER; file điều phối lưu bền ngoài `/tmp`; đo `--durations` trước khi tối ưu test |
| 30/10/2026 | P4.1 vòng 2 (recheck cap): **người dùng chốt** bộ đếm theo từng `incident_id`, incident con bắt đầu bộ đếm riêng. Định nghĩa khoảng cách ≥ 24 giờ giữa hai `as_of_time` UTC liên tiếp do implementer đặt theo `docs/triage.md` mục 2 ("cách nhau một ngày"), reviewer xác nhận khớp nhịp chạy hằng ngày; recheck thứ ba hoặc quá sớm chuyển `human_review` |
| 30/10/2026 | BACKLOG-5 round 1: `unit_or_null_change` kiểm tra `n_min` trên từng ngày pre/post trước khi chấp nhận candidate; `n_min` chọn và khóa trên development theo [triage.md](docs/triage.md) mục 3 |
| 29/10/2026 | Người dùng khóa ngân sách benchmark 10 USD (gồm 4 baseline + ablation), giữ 60 episode; tăng episode chỉ khi duyệt lại sau pilot 60 |
| 29/10/2026 | P4.6: smoke chỉ có 2 incident tới agent ngoài test → người dùng chọn đo 2 ca thay vì 3 (ca thứ ba hoãn tới pilot 60); 4 lời gọi thật, 13.217 token vào / 642 ra, ≈ 0,0005 USD; chưa khóa trần benchmark và số episode. Mốc ★ đạt |
| 27/10/2026 | Người dùng đổi Reviewer sang Claude Opus 5.5 (sub-agent mới mỗi lượt review); `gpt-6.1-sol` thành dự phòng |
| 27/10/2026 | Người dùng chốt runbook `unit_or_null_change`: tỉ số trung vị sau ÷ trước dùng dải log ×/÷2 quanh {100, 1/100, 1000} thay ± 5% (± 5% không đạt được với cỡ mẫu ngày nhỏ; xem [triage](docs/triage.md) mục 3). Tạo policy version mới |
| 26/10/2026 | Người dùng chốt LLM agent: Qwen 3.7 Flash qua OpenRouter (`qwen/qwen3.7-flash`, API key trong `.env`, không commit); thay DeepSeek V4 Flash. Rule P4 vòng 1 `unit_or_null_change` (planner và GPT-6.1-sol cùng đề xuất). `decision_at = as_of_time` |
| 26/10/2026 | Làm rõ cửa sổ incident P3.3: mốc 2 ngày tính từ alert gần nhất; alert sau quyết định cuối chỉ tạo incident con trong 2 ngày, xa hơn là incident độc lập. Cập nhật kiểm chứng protocol và triển khai vòng 1. |
| 25/10/2026 | Claude tự điều phối theo docs/orchestration.md: goal P1→P2→P3 (vòng 1 cả ba tới ★, rồi vòng 2), chạy liên tục; tự merge khi review PASS + test xanh; dừng hỏi người dùng theo bảng ranh giới |
| 25/10/2026 | Flow agent: Claude Opus plan + điều phối qua herdr; Codex `gpt-6-luna` (max) code trong worktree theo task; Codex `gpt-6.1-sol` (high) review read-only qua `scripts/review_task.sh`, tối đa 2 vòng; người dùng merge. P1 làm hai vòng (vòng 1 tối thiểu tới mốc ★, vòng 2 làm sâu) — xem docs/p1-implementation.md |
| 25/10/2026 | Thêm sub-plan P1 theo task có đầu vào/phụ thuộc, file, kiểm tra và điều kiện hoàn tất; schema vẫn chỉ ở docs/data.md. Chưa triển khai runtime; không thay đổi protocol MVP |
| 25/10/2026 | Sửa sau review rule/agent: mùa vụ dùng nền theo ngày và aggregation theo metric; rủi ro so policy dùng tập truth/episode cố định, reward không tăng do incident con; tách nhánh close và routing; chốt giới hạn giá trị agent trong 4 runbook MVP. Bổ sung ví dụ kiểm chứng; chưa triển khai runtime |
| 25/10/2026 | Tổ chức lại tài liệu theo module (`docs/`); MASTER chỉ giữ đích đến, trạng thái, lộ trình, phạm vi MVP, ngân sách, nhật ký. PROJECT_PLAN, RESEARCH_DECISIONS, PLAN_REVIEW và bản MASTER cũ chuyển vào `archive/`. Gộp P1 + P1.5 thành P1 |
| 25/10/2026 | Sửa sau review lần 3: so sánh gate UQ dùng cùng kết luận đa số + bằng chứng, chỉ bật/tắt điều kiện score; routing đúng hạn kiểm tra đúng incident và `intervention_start_at <= decision_at <= deadline_at` |
| 25/10/2026 | Sửa sau review lần 2: runbook mùa vụ kiểm tra vị trí + độ lớn theo ngày thay cho độ phủ ≥ 50%; hai phép so sánh agent (gate UQ / giá trị vận hành K = 1 vs K = 3); timely routing có `accepted_actions` + `deadline_at` (D = 7), recheck không tính routing |
| 25/10/2026 | Sửa sau review: tách quyền tự đóng rule (theo runbook) và agent (tau); incident con + chấm theo `decision_at`; checklist runbook bằng code; ngân sách tạm → đo trên luồng mỏng → khóa; gắn nhãn MVP/mở rộng tại từng module |
| 25/10/2026 | Code P1/P2 bản 24/10 rollback → làm lại. Khóa protocol, chốt phạm vi MVP/làm sau. Rút split model từ 4 xuống 3 tập: fit 1/7–21/9, validation 22–30/9, calibration tháng 10 dùng chung cho isotonic + conformal (score thô) + CBPE. Ngân sách quyết định số episode, 60 là sàn |
| 25/10/2026 | Chốt protocol nhãn H = 90 theo cohort, pending không âm hóa, tách observed-label/oracle và các vai trò calibration |
| 25/10/2026 | UQ agent MVP dùng abstention thực nghiệm + gate chung rule/agent; conformal agent thành nhánh bổ sung; conformal giao dịch giữ ở P2 |
| 25/10/2026 | Pilot 12/24/24 development/calibration/test; metric end-to-end, attribution ambiguous/unmatched, rủi ro theo mẫu số; chưa có chứng nhận gần 0. Taxonomy bỏ `insignificant`, mẫu nhỏ là `evidence_status = insufficient` |
| 24/10/2026 | Định vị theo business problem (alert fatigue), không theo tính mới |
| 24/10/2026 | Triage hybrid: rule trước, agent sau; rule-based là baseline chính |
| 24/10/2026 | SQL 40/30/15/15 · user simulator để sau · ma trận chi phí (−5/−3/−1/0/+1) + độ nhạy · demo video + Docker + Streamlit chỉ đọc |
| 24/10/2026 | LLM: DeepSeek V4 Flash; UQ black-box trước (thay bằng Qwen 3.7 Flash ngày 26/10) |
| 24/10/2026 | Simulator v0: collusion chỉ trước mốc drift để giai đoạn drift có một nguyên nhân |
| 24/10/2026 | Simulator: bản đầy đủ vai trò tài khoản (hộ kinh doanh, mule nội bộ có cash-out), thêm sự cố bẫy `legit_business_spike` |
| 24/10/2026 | Chốt chia dữ liệu theo độ chín của nhãn (T0 = 1/2/2026); 60 episode "thế giới song song" + `business_calendar`; alert 4 tầng, chấm theo incident |
| 24/10/2026 | P2: conformal Mondrian bất đối xứng (α_legit 0,005, α_fraud 0,20); huấn luyện tất định |
| 24/10/2026 | Audit nghiệp vụ simulator mức 1+2 (thiết bị hợp lệ thay đổi, chữ ký ATO/card testing theo xác suất, nhãn theo kênh + friendly fraud, Tết đổi cơ cấu, mule thuê, refund/reversal, merchant quen, retry); thêm leakage check |
