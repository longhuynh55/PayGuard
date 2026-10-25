#!/usr/bin/env bash
# Run one read-only, fresh-context Codex review of a task branch.
#
# Usage (from the task worktree): scripts/review_task.sh <task-id> <round> [base]
#   scripts/review_task.sh P1.1 1          # diff main...HEAD
# Output: review/findings/<task-id>-r<round>.md
# The orchestrator runs tests first and saves them to review/findings/<task-id>-tests.txt.
set -euo pipefail

task="${1:?task id, e.g. P1.1}"
round="${2:?review round, 1 or 2}"
base="${3:-main}"

# Round 3+ only with explicit user approval for this task: ALLOW_EXTRA_ROUND=1.
if [[ "$round" != "1" && "$round" != "2" && "${ALLOW_EXTRA_ROUND:-0}" != "1" ]]; then
  echo "AGENTS.md allows at most 2 review rounds per task; ask the user." >&2
  exit 2
fi

mkdir -p review/findings
out="review/findings/${task}-r${round}.md"
tests="review/findings/${task}-tests.txt"

prompt=$(cat <<EOF
Bạn là REVIEWER của task ${task}, vòng ${round}. Đọc AGENTS.md và làm đúng vai Reviewer.

Phạm vi review:
- Diff: \`git diff ${base}...HEAD\` (và \`git status\` cho file chưa commit).
- Task spec: mục ${task} trong docs/p1-implementation.md, kể cả bảng "Thứ tự: hai vòng".
- Hợp đồng: các file docs/ mà task link tới. archive/ KHÔNG dùng làm căn cứ.
- Kết quả test do orchestrator chạy: ${tests} (nếu có). KHÔNG tự chạy lệnh ghi file.

Kiểm tra:
1. Từng tiêu chí nghiệm thu của task (vòng hiện tại) có đạt và có test chứng minh không.
2. Bất biến trong AGENTS.md: AS-OF, TRUTH CÔ LẬP, PENDING KHÔNG PHẢI NHÃN ÂM, KHÔNG TUNE TRÊN TEST.
3. Lệch giữa code và docs/; test nghiệm thu có bị nới/xóa không.
4. Làm lan ra ngoài phạm vi task hoặc vào phần Mở rộng.

Trả lời bằng tiếng Việt, đúng định dạng:
## Kết luận
PASS | CHANGES_REQUIRED
## Findings
- [CHẶN MVP | KHÔNG CHẶN] file:line — vấn đề. Bằng chứng: phản ví dụ hoặc lệnh đã chạy. Đề xuất sửa.
(Không có finding thì ghi "Không có".)
## Tiêu chí nghiệm thu
- [x]/[ ] từng tiêu chí, kèm test chứng minh.
KHÔNG mở vòng research mới. KHÔNG sửa file.
EOF
)

codex exec \
  -m gpt-6.1-sol \
  -c model_reasoning_effort="high" \
  -s read-only \
  --ephemeral \
  -o "$out" \
  "$prompt"

echo "Findings: $out"
grep -m1 -E "^(PASS|CHANGES_REQUIRED)" "$out" || echo "WARNING: no verdict line found in $out" >&2
