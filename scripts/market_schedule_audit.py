"""Audit completed half-hour slots from GitHub's real schedule event records."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SLOT = timedelta(minutes=30)


def utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def slot_at(moment):
    return moment - timedelta(minutes=(moment.minute - 7) % 30,
                              seconds=moment.second, microseconds=moment.microsecond)


def audit(runs, now):
    # A watchdog is scheduled 15 minutes after its primary.
    last_slot = slot_at(now - timedelta(minutes=15))
    slots = {last_slot - offset * SLOT: [] for offset in range(48)}
    for run in runs:
        if run.get("event") != "schedule" or run.get("display_title") != "Market refresh primary":
            continue
        observed_slot = slot_at(utc(run["created_at"]))
        if observed_slot in slots:
            slots[observed_slot].append(run)
    records = []
    for slot, runs in sorted(slots.items()):
        conclusions = [r.get("conclusion") for r in runs]
        if "success" in conclusions:
            status = "success"
        elif any(c is None for c in conclusions):
            status = "running"
        else:
            status = "failed" if runs else "unobserved"
        records.append({"slot_utc": slot.isoformat(), "status": status,
                        "runs": [{k: run.get(k) for k in ("id", "created_at", "run_started_at", "html_url", "conclusion")}
                                 for run in runs]})
    counts = {state: sum(row["status"] == state for row in records)
              for state in ("success", "failed", "running", "unobserved")}
    return {"checked_at_utc": now.isoformat(), "expected_slots": len(records), **counts,
            "last_two_primary_slots_successful": all(row["status"] == "success" for row in records[-2:]),
            "full_window_successful": counts["success"] == len(records), "slots": records,
            "note": "Slots use the GitHub-created timestamp. A run created after its intended slot may count as late or unobserved. Pre-rollout slots also appear unobserved."}


def main():
    runs = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["workflow_runs"]
    result = audit(runs, datetime.now(timezone.utc))
    Path(sys.argv[2]).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
        summary.write("### Rolling 24-hour scheduled run audit\n\n")
        summary.write(" / ".join(f"{key}: {result[key]}" for key in
                                 ("expected_slots", "success", "failed", "running", "unobserved")) + "\n\n")
        summary.write(f"Last two primary slots succeeded: {result['last_two_primary_slots_successful']}\n")
    if result["slots"][-1]["status"] == "success":
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write("skip=true\n")


if __name__ == "__main__":
    main()
