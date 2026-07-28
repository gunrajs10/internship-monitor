"""Union-merge two copies of state/seen.json.

Why this exists: two monitor runs that overlap both append to the dedupe
ledger, and `git pull --rebase` then reports a content conflict in
state/seen.json. That conflict is not a real disagreement - the ledger is
append-only, so the correct resolution is always "keep both sides". Textual
rebase cannot know that, so it fails the run and the whole run's state is
lost, which silently causes every posting from that run to be re-alerted
later.

Usage:  python3 merge_state.py <theirs.json> <ours.json>
Writes the union back to <ours.json>. Our copy wins on a key collision,
since it is the fresher observation of the same posting.
"""

import json
import sys


def load(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def main():
    if len(sys.argv) != 3:
        print("usage: merge_state.py <theirs.json> <ours.json>", file=sys.stderr)
        return 2
    theirs_path, ours_path = sys.argv[1], sys.argv[2]
    theirs, ours = load(theirs_path), load(ours_path)

    merged = {}
    for section in ("seen", "failures"):
        combined = dict(theirs.get(section) or {})
        combined.update(ours.get(section) or {})
        merged[section] = combined

    # Preserve any section a future version of monitor.py adds, so this
    # script does not quietly truncate state it does not know about.
    for src in (theirs, ours):
        for key, value in src.items():
            if key not in merged:
                merged[key] = value

    with open(ours_path, "w") as f:
        json.dump(merged, f, indent=1, sort_keys=True)

    print(f"merged state: {len(merged['seen'])} seen, "
          f"{len(merged['failures'])} failures "
          f"(theirs {len(theirs.get('seen') or {})}, "
          f"ours {len(ours.get('seen') or {})})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
