# HARDEN-1 — Review độc lập vòng 1

**VERDICT: CHANGES_REQUIRED.** Ba lỗi chặn nghiệm thu HARDEN-1: config standalone có thể vượt lock khi chạy/chấm test; phép chiếu `on` chưa dùng đúng khóa `(feature, segment)` đã duyệt; cap P4 provider bị nâng từ $1 lên $10 dù giá trị này còn khóa. Có thêm một lệch docs không chặn.

- Reviewer: Codex supervisor reviewer, read-only đối với repo/worktree. Không sửa code/docs/test, không stage/commit/merge, không gửi lệnh cho implementer.
- Cây review: `/home/lieu_hoai_phuc/.herdr/worktrees/Aiagent/slice-harden-1`, HEAD `5a37211e85d8b53f5b1d3792432b8c174ba14924`. Review **`git diff HEAD` và untracked `payguard/run_identity.py`**, không dùng diff commit rỗng làm căn cứ.
- Mọi hash trong `takeover/state.json.source_hashes` khớp trước/sau review và full suite; HEAD/status không đổi, diff và run_identity snapshot trùng từng byte. **Review không stale**. Bằng chứng: `takeover/harden1-r1-probes/source-check-final.json`.
- Đã đọc AGENTS, MASTER, task prompt, HARDEN-1, data §8–10, evaluation §1/9, audit A2/A3/B1/B2/C1/C5b, cả hai bản P5.0b r2. Báo cáo này không thay đổi verdict lịch sử hoặc giải quyết thay người dùng bất đồng xếp mức P5.0b.

## R1-1 — CHẶN MVP/pilot: `--triage-config`/config standalone không thuộc identity đã khóa

**Vị trí:** `triage/__main__.py:86–110` trả về provider cho standalone trước nhánh kiểm config path tại dòng 111–124; `triage/__main__.py:238–247` kiểm role bằng identity mặc định rồi chạy runner với `args.triage_config`. `payguard/identity.py:178–184` chỉ băm các config trong repo. `evaluation/__main__.py:180–205` đối chiếu version/model hiện tại nhưng không thể nhận ra threshold từ config ngoài lock khi giữ nguyên policy_version.

**Phản ví dụ thật:** dùng bản copy BASE smoke có scores/metrics/alerts từ audit vào `/tmp/payguard-harden1-review-probes/custom-config`. Tạo lock `rule_human` cho config mặc định; chỉ mock `_dirty_entries=()` lúc tạo lock để mô phỏng cây sau commit (đây cũng là cách test lock của task xử lý worktree chưa commit). Không mock hash, loader, role guard, runner, evaluator hay API LLM.

Script và log lưu bền tại `takeover/harden1-r1-probes/probe_config.{py,log}`. Lệnh đã chạy từ worktree:

```bash
PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/tmp/payguard-uv-cache uv run python /tmp/payguard-harden1-review-probes/probe_config.py
```

Script gọi CLI công khai tương đương:

```bash
python -m triage run --run /tmp/payguard-harden1-review-probes/custom-config --episode-role test --triage-config /tmp/payguard-harden1-review-probes/custom-triage.yaml
```

- Lock mang `policy_version=payguard.triage.p4.round2.v2`.
- Custom config có `policy_version=unlocked-custom-policy-v999`, `small_sample.n_min=100000`.
- **Actual:** exit 0; ghi **17 decisions của role test** với `policy_version=unlocked-custom-policy-v999` (lock không khớp policy thực thi).
- Ở bản copy mới, giữ nguyên policy_version mặc định nhưng vẫn đổi `n_min=100000`: triage test tiếp tục exit 0. Sau đó `python -m evaluation decisions --run /tmp/payguard-harden1-review-probes/same-version-config --episode-role test` cũng exit 0, `decision_count=17`, chấm `ep-0004`, `ep-0005`.
- Đối chiếu action test với fixture mặc định của audit: `1 data_ticket + 16 human_review` → `17 human_review` sau đổi n_min ngoài lock. Đây là thay đổi hành vi policy thực tế, không chỉ thay metadata.
- **Expected:** từ chối config/policy chưa được lock trước execution và trước truth; không ghi quyết định hay report test.

**Hệ quả:** có thể chỉnh ngưỡng qua config ngoài repo, giữ version, chạy lại/chấm test với một lock không thay đổi. Đây là lỗ thực thi của guard KHÔNG TUNE TRÊN TEST, không phải chỉ thiếu thông báo lỗi.

**Đề xuất:** standalone held-out từ chối custom simulator/triage/LLM config không thuộc lock, hoặc đưa nội dung/effective policy của các config thực sự dùng vào một persisted identity và kiểm cùng identity ở cả execution/evaluation. Kiểm policy_version thực tế trước runner; chỉ so version là chưa đủ vì phản ví dụ giữ version cũng chấm thành công. Áp cùng nguyên tắc cho `triage measure`. Functional test phải dùng config ngoài lock, cả đổi version lẫn giữ version/đổi threshold; assert runner/truth không chạy và DB/report không đổi khi bị từ chối.

## R1-2 — CHẶN nghiệm thu HARDEN-1: `on` vẫn so `start_at`, trái khóa so sánh đã duyệt

**Vị trí:** `evaluation/support.py:239–245`, nhất là dòng 240; `docs/data.md:427` cũng thêm `start_at` vào hợp đồng. HARDEN-1 trong `docs/p5-implementation.md` yêu cầu rõ **chỉ `(feature, segment)`**, kèm `rule.decision_at <= agent.decision_at`.

Script và log lưu bền tại `takeover/harden1-r1-probes/probe_scope.{py,log}`; fixture dựng bằng helper tests và writer công khai, freeze BASE/manifests đầy đủ, gọi CLI `evaluation support` thật, không mock evaluator/identity. Lệnh đã chạy:

```bash
PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/tmp/payguard-uv-cache uv run python /tmp/payguard-harden1-review-probes/probe_scope.py
```

Ba fixture đều có một incident, rule/agent seasonal checklist pass, cùng `feature=txn_count, segment=all`:

| Fixture | Rule/agent | Off | On actual | On expected |
|---|---|---:|---:|---:|
| `same_start` | rule trước agent, cùng start_at, khác end_at | 1 | 0 | 0 |
| `different_start` | rule trước agent, chỉ start_at của agent khác | 1 | **1** | **0** |
| `future_rule` | rule sau agent | 1 | 1 | 1 |

Mọi CLI exit 0, count sai có `reason=null`. Bản sửa đã giải quyết phản ví dụ cũ chỉ khác `end_at` và kiểm đúng thứ tự rule/agent, nhưng chưa thực hiện khóa so sánh đã duyệt. Test mới `tests/test_evaluation_support.py:572` vẫn cố định `same_scope_start` cho hai decision nên bỏ sót ca này.

**Đề xuất:** bỏ `start_at` khỏi phép so sánh; giữ điều kiện thời gian đã thêm. Đồng bộ data §10 về `(feature, segment)`; thêm functional case rule trước agent/cùng feature-segment/khác start_at và case rule tương lai, không nới assertion cũ. Mức chặn ở đây là chưa hoàn thành nghiệm thu task đã duyệt; không tự diễn giải lại verdict P5.0b lịch sử.

## R1-3 — CHẶN: tự nâng cap P4 provider đã khóa từ $1 lên $10

**Vị trí:** `triage/__main__.py:90–103` không chỉ tạo shared ledger $10 mà còn truyền `cost_cap_usd=BENCHMARK_COST_CAP_USD` cho provider standalone; `triage/agent.py:347–348` dùng override này thay `max_total_cost_usd` trong config. `configs/llm.yaml` vẫn khóa `budget.max_total_cost_usd=1.0`; MASTER §5, `docs/p5-implementation.md:98` và `docs/triage.md:107` phân biệt cap P4 tạm $1 với cap toàn benchmark $10.

**Phản ví dụ đã chạy, không API:** `takeover/harden1-r1-probes/probe_caps.{py,log}` chỉ dựng provider thật qua `_build_provider_for_run`, không gọi `propose`/`complete_json`:

```bash
PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/tmp/payguard-uv-cache uv run python /tmp/payguard-harden1-review-probes/probe_caps.py
```

Kết quả: `CONFIG_P4_PROVIDER_CAP=1.0`, **`ACTUAL_P4_PROVIDER_CAP=10.0`**, `SHARED_BENCHMARK_LEDGER_CAP=10.0`. Vì vậy cả trần in-memory per-provider lẫn trần ledger đều thành $10; không còn lớp chặn P4 $1. Test mới `tests/test_payguard_harness.py:472` còn assert override 10, tức khóa hành vi nâng cap chưa được duyệt.

**Đề xuất:** giữ shared durable ledger ở cap benchmark $10 nhưng không override cap P4 provider $1 cho standalone (`run` và `measure`); hoặc từ chối standalone provider thật theo lựa chọn B2 cho phép. Policy harness benchmark có thể tiếp tục dùng override $10. Functional test phải kiểm cả hai trần riêng biệt. Không chỉ sửa docs để hợp thức hóa việc nâng một giá trị đã khóa. HARDEN-1 yêu cầu ledger bền/hoặc từ chối, không tự cấp quyền đổi cap P4.

## R1-4 — KHÔNG CHẶN: mặc định `model evaluate` trong harness khác docs

**Vị trí:** `model/__main__.py:111–116` mặc định chọn **mọi role** khi tìm thấy harness; `docs/model.md:52` nói mặc định chỉ development và calibration/test cần chọn bằng `--episode-role`.

**Hệ quả:** trong harness, lệnh không selector có thể chạm calibration/test nếu lock tồn tại, hoặc từ chối vì thiếu lock thay vì chấm development theo docs. Guard vẫn chạy trước truth.

**Đề xuất:** thống nhất mặc định development như docs (hoặc ghi đúng ngoại lệ harness trong docs nếu đó là hành vi đã chốt); thêm functional test mặc định trong harness để giữ hợp đồng.

## Kiểm tra và kết quả

| Kiểm tra | Kết quả |
|---|---|
| Full suite độc lập đúng một lần | **408 passed, 1 xfailed**, exit 0, 666.87s; strict xfail seasonal đã biết. Log: `takeover/harden1-r1-probes/full-suite-summary.log` |
| `simulator generate --profile smoke --out /tmp/payguard-harden1-review-smoke` | Exit 0, validator tích hợp 16/16 |
| `simulator validate --run /tmp/payguard-harden1-review-smoke` | Exit 0, 16 passed / 0 failed |
| `verify_plan_claims.py` trên bản copy script /tmp | 19 ví dụ; JSON kết quả trùng file repo; không gọi verifier ghi vào repo |
| `python3 review/check_links.py` | 16 files, 0 broken links |
| `git diff --check HEAD` | Exit 0 |
| Fresh process `import triage.__main__` | Không có module evaluation trong sys.modules |
| Probe config standalone/held-out | Tái hiện R1-1; không gọi API thật |
| Probe support scope/time | Tái hiện R1-2; đối chứng khác end_at và rule tương lai đúng |

Lệnh full suite đã dùng nguyên một lần:

```bash
PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/tmp/payguard-uv-cache uv run python -m pytest -q --durations=15 -o cache_dir=/tmp/payguard-harden1-review-cache --basetemp=/tmp/payguard-harden1-review-tmp
```

## Checklist nghiệm thu

- [x] A2: harness evaluator kiểm current/run-manifest/lock hashes, identity policy/model và fingerprint policy copy = BASE trước truth. Regression code A/run A → lock B bị chặn.
- [x] A2: harness default development không triage calibration/test khi chưa có lock; episode IDs thực sự truyền vào runner theo role.
- [x] A3: `evaluation decisions` standalone tôn trọng selector, mặc định development; explicit held-out yêu cầu guard.
- [x] A3: `model evaluate` standalone mặc định development, từ chối held-out; harness có identity/role guard trước truth.
- [ ] Guard standalone gắn với **config thực dùng** khi execution/analysis held-out — R1-1.
- [x] B1: resume harness/BASE/policy và triage so content identity, không chặn bởi Git HEAD/dirty metadata; lỗi nêu trường hash lệch.
- [x] B2: có policy OpenRouter K1 qua shared durable ledger; standalone OpenRouter nối ledger chung, không gọi API thật trong test/review.
- [ ] Bảo toàn cap P4 provider $1 đồng thời dùng shared benchmark ledger $10 — R1-3.
- [x] B2: lock benchmark từ root smoke bị từ chối; pilot chuẩn hóa benchmark.
- [x] C1: triage không import evaluation gián tiếp; có subprocess boundary test mới.
- [x] C5b: test harness dùng synthetic Git metadata, có docs-only HEAD/scratch-file resume case.
- [ ] N1: on chỉ so feature/segment và kiểm thời gian rule trước agent — phần thời gian đạt, còn start_at R1-2.
- [x] N2: by_truth_group có off/on, count null kèm reason khi trạng thái aggregate tương ứng không đo được.
- [x] N3: sinh hàng cho mọi nhóm truth development, kể cả không có match.
- [x] N4: code-owned `__runner_guard` lưu runner_tool_error/invalid_snapshot; evaluator tính forced-human; reserved key từ proposal bị bỏ trước code tính.
- [x] Support dùng helper gate public; thiếu/invalid checklist status ném DevelopmentSupportError.
- [x] Contract/schema mới có ở data §8–10; evaluation §9 cập nhật cùng diff. Còn lệch R1-2/R1-4 cần đồng bộ; R1-3 phải khôi phục cap đúng phạm vi.
- [ ] Không đổi giá trị khóa: simulator/split/threshold không đổi nhưng cap P4 bị override $1 → $10 (R1-3).
- [x] Không xóa/nới assertion nghiệm thu. Những assertion đổi số lượng model/test CLI phù hợp default development và thêm explicit role/locks, còn assertions kết quả cũ được giữ.
- [x] AS-OF/truth isolation/pending/evidence gate: không thấy regression từ diff; smoke validator xác nhận AS-OF, truth boundary, pending không âm; gate vẫn do code tính.
- [x] Full suite độc lập hoàn tất đúng một lần: 408 passed, 1 xfailed; không có sandbox/dependency failure.

Suite xanh nhưng chưa đủ điều kiện merge; cần sửa ba finding chặn và review vòng 2 theo giới hạn AGENTS.
