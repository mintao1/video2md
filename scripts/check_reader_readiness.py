"""Fail closed when real reader readiness within 60s is not demonstrated."""

import json
import sys
from datetime import datetime
from pathlib import Path


def evaluate(e, *, allow_synthetic=False):
    try:
        if e["kind"] != "production" and not (
            allow_synthetic and e["kind"] == "synthetic"
        ):
            return False
        start, ready = (
            datetime.fromisoformat(e[k]) for k in ("opened_at", "verified_ready_at")
        )
        if (
            start.tzinfo is None
            or ready.tzinfo is None
            or not 0 <= (ready - start).total_seconds() <= 60
        ):
            return False
        expected = e["expected_videos"]
        rows = e["videos"]
        return (
            bool(e["run_id"])
            and bool(expected)
            and len(set(expected)) == len(expected)
            and e["measurement_complete"] is True
            and e["pending"] == []
            and len(rows) == len(expected)
            and {r["label"] for r in rows} == set(expected)
            and all(
                type(r["readyState"]) is int
                and r["readyState"] >= 2
                and type(r["error"]) is int
                and r["error"] == 0
                for r in rows
            )
        )
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


if __name__ == "__main__":
    result = evaluate(json.loads(Path(sys.argv[1]).read_text()))
    print(
        "PASS: reader media ready within 60s"
        if result
        else "FAIL: bounded real reader readiness is not demonstrated"
    )
    raise SystemExit(0 if result else 1)
