"""Check that relative Markdown links in MASTER_PLAN.md, AGENTS.md and docs/ point to existing files.

Run from the project root: python3 review/check_links.py
"""

import re
import sys
from pathlib import Path

LINK = re.compile(r"\]\(([^)]+)\)")


def main():
    files = [Path("MASTER_PLAN.md"), Path("AGENTS.md"), *sorted(Path("docs").glob("*.md"))]
    broken = []
    for source in files:
        if not source.exists():
            continue
        for match in LINK.finditer(source.read_text(encoding="utf-8")):
            target = match.group(1).split("#")[0]
            if not target or target.startswith(("http://", "https://")):
                continue
            if not (source.parent / target).exists():
                broken.append(f"{source}: {target}")
    for line in broken:
        print("broken link:", line)
    print(f"Checked {len(files)} files, {len(broken)} broken links.")
    sys.exit(1 if broken else 0)


if __name__ == "__main__":
    main()
