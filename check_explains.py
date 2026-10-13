"""Structural explain checker.

Reads the five explain_*.json files produced by /admin/explain/... and
verifies each plan contains an IXSCAN in the winning plan, no COLLSCAN
anywhere, and prints examined/returned counts. Exits non-zero on failure.
"""
import json
import pathlib
import sys


def walk(obj):
    """Yield every dict in a nested structure."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk(item)


def analyze(explain_doc):
    ix = coll = False
    examined = returned = None
    plan_keys = set()

    # Collect "stage" keys and the specific fields we care about.
    for d in walk(explain_doc):
        stage = d.get("stage")
        if stage == "IXSCAN":
            ix = True
            plan_keys.add("IXSCAN")
        elif stage == "COLLSCAN":
            coll = True
            plan_keys.add("COLLSCAN")
        elif stage:
            plan_keys.add(stage)
        if "totalDocsExamined" in d and examined is None:
            examined = d["totalDocsExamined"]
        if "nReturned" in d and returned is None:
            returned = d["nReturned"]

    return {
        "ixscan": ix,
        "collscan": coll,
        "examined": examined,
        "returned": returned,
        "stages": sorted(plan_keys),
    }


def check(path: str) -> bool:
    try:
        doc = json.loads(pathlib.Path(path).read_text())
    except FileNotFoundError:
        print(f"FAIL {path}: file missing")
        return False
    except json.JSONDecodeError as e:
        print(f"FAIL {path}: invalid JSON ({e})")
        return False

    if "explain" not in doc:
        print(f"FAIL {path}: missing 'explain' key")
        return False

    info = analyze(doc["explain"])
    ok = info["ixscan"] and not info["collscan"]
    status = "OK " if ok else "FAIL"
    examined = info["examined"]
    returned = info["returned"]
    ratio = f" (examined/returned = {examined}/{returned})" if examined is not None and returned else ""
    print(f"{status} {path}: IXSCAN={info['ixscan']} COLLSCAN={info['collscan']}{ratio}")
    print(f"       stages: {info['stages']}")
    return ok


FILES = [
    "explain_attendance_list.json",
    "explain_employee_monthly.json",
    "explain_department_summary.json",
    "explain_late_leaderboard.json",
    "explain_department_trend.json",
]


if __name__ == "__main__":
    results = [check(f) for f in FILES]
    print()
    if all(results):
        print("All explains OK.")
        sys.exit(0)
    print(f"{sum(results)}/{len(results)} passed.")
    sys.exit(1)