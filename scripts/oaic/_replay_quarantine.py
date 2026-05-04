"""Replay every quarantined payload through the new validator code to
see how many would now pass. Lets us verify the fixes empirically
without re-running the (slow, expensive) Playwright + vision pipeline.

For each quarantine entry:
  - read payload.json
  - infer the requested semester from the parent dirname
  - run validate_page_payload with the new auto-swap adapter
  - print PASS / FAIL + reason
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.oaic.oaic_validators import (
    OAICValidationError,
    validate_displayed_semester,
    validate_page_payload,
)


PAGE_FROM_DIRNAME = {
    "page2": 2, "page3": 3, "page4": 4, "page5": 5,
    "page6": 6, "page7": 7, "page8": 8, "page9": 9,
}


def main() -> int:
    root = Path("instance/oaic_debug")
    if not root.exists():
        print(f"No quarantine dir at {root}")
        return 0
    pass_count = 0
    fail_count = 0
    fail_by_page: Counter = Counter()
    fail_reasons: Counter = Counter()

    for sem_dir in sorted(root.iterdir()):
        if not sem_dir.is_dir():
            continue
        semester = sem_dir.name.replace("_", " ")
        for entry in sorted(sem_dir.iterdir()):
            if not entry.is_dir():
                continue
            page_part = entry.name.split("_")[0]
            page = PAGE_FROM_DIRNAME.get(page_part)
            if not page:
                continue
            payload_path = entry / "payload.json"
            if not payload_path.exists():
                continue
            try:
                payload = json.loads(payload_path.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"[SKIP] {entry}: cannot read payload ({e})")
                continue
            try:
                validated = validate_page_payload(page, payload, semester)
                validate_displayed_semester(page, validated, semester)
                pass_count += 1
                # Show a short success line for the cases that previously
                # failed with the swap pattern.
                if (payload.get("current_period_label") and
                    payload.get("previous_period_label") and
                    payload.get("current_period_label") != validated.get("current_period_label")):
                    print(f"  RESCUED [page {page}] [{semester}] swap auto-corrected")
            except OAICValidationError as e:
                fail_count += 1
                fail_by_page[page] += 1
                # First error string only, truncated.
                first_err = (e.errors[0] if e.errors else "?")[:120]
                fail_reasons[first_err.split(":")[0][:50]] += 1
                print(f"  STILL FAIL [page {page}] [{semester}]: {first_err}")
            except Exception as e:
                fail_count += 1
                print(f"  ERROR [page {page}] [{semester}]: {e}")

    total = pass_count + fail_count
    print(f"\n=== Replay summary: {pass_count}/{total} now pass "
          f"({fail_count} still fail) ===")
    if fail_by_page:
        print("\nStill-failing by page:")
        for p, c in sorted(fail_by_page.items()):
            print(f"  page {p}: {c}")
    if fail_reasons:
        print("\nStill-failing top reasons:")
        for r, c in fail_reasons.most_common(8):
            print(f"  {c}x: {r}")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
