"""Inspect recorded batches without rerunning models or reading current prompts."""
from collections import Counter
import json
from statistics import mean


def enrich_report(recorder, report, batch, snapshot):
    report["excluded_cases"] = snapshot["excluded_cases"]
    report["act"] = snapshot["act"]
    report["model"] = snapshot["model"]
    expected = {(c, n) for c in json.loads(batch["cases_json"]) for n in range(1, batch["repeats"] + 1)}
    actual = {(r["case_id"], r["repetition"]) for r in report["runs"]}
    report["missing_runs"] = [{"case_id": c, "repetition": n} for c, n in sorted(expected - actual)]
    for run in report["runs"]:
        rows = recorder.query("SELECT payload_json FROM events WHERE run_id=? AND event_type='evaluation.observation' ORDER BY sequence DESC LIMIT 1", (run["run_id"],))
        run["observation"] = json.loads(rows[0]["payload_json"]) if rows else None
        run["version_id"] = recorder.query("SELECT version_id FROM runs WHERE run_id=?", (run["run_id"],))[0]["version_id"]
    report["errors"] = sum(r["run_status"] in {"error", "stopped", "running"} or any(c["status"] == "error" for c in r["checks"]) for r in report["runs"])
    report["failed"] = sum(not r["passed"] and r["run_status"] in {"completed", "blocked"} and not any(c["status"] == "error" for c in r["checks"]) for r in report["runs"])
    report["pass_rate"] = report["passed"] / report["expected_runs"]
    report["case_summary"] = []
    for case_id in json.loads(batch["cases_json"]):
        runs = [r for r in report["runs"] if r["case_id"] == case_id]
        observations = [r["observation"] for r in runs if r["observation"]]
        durations = [o["duration_seconds"] for o in observations if o["duration_seconds"] is not None]
        variants = {}
        for o in observations:
            entry = variants.setdefault(o["output_fingerprint"], {"count": 0, "output": o["normalized_output"]})
            entry["count"] += 1
        report["case_summary"].append({"case_id": case_id, "passed": sum(r["passed"] for r in runs),
            "scheduled": batch["repeats"], "recorded": len(runs), "output_variants": list(variants.values()),
            "distinct_tool_paths": len({tuple(o["tool_path"]) for o in observations}),
            "distinct_response_texts": len({o["response_fingerprint"] for o in observations if o.get("response_fingerprint")}),
            "latency_seconds": {"mean": mean(durations), "min": min(durations), "max": max(durations)} if durations else None,
            "model_calls": [o["model_calls"] for o in observations],
            "token_usage": [o["usage"] for o in observations],
            "check_counts": dict(Counter(c["status"] for r in runs for c in r["checks"])),
            "unexercised_checks": [c["check_id"] for r in runs for c in r["checks"]
                                   if c["status"] == "not_applicable" and c["check_id"].startswith("coverage.")]})
    report["interpretation"] = "Pass rate uses all scheduled runs. Variants measure structured outcomes, not prose identity. Human prose review is separate; small samples do not establish reliability."


def changed_paths(a, b, prefix=""):
    if isinstance(a, dict) and isinstance(b, dict):
        return [p for key in sorted(set(a) | set(b)) for p in changed_paths(a.get(key), b.get(key), f"{prefix}.{key}" if prefix else key)]
    return [prefix] if a != b else []


def enrich_comparison(answer, before, after, snapshots):
    answer["changed_paths"] = changed_paths(*snapshots)
    answer["batch_results"] = [{"label": r["label"], "mode": r["mode"], "passed": r["passed"], "scheduled": r["expected_runs"],
                                "missing": len(r.get("missing_runs", [])), "case_summary": r.get("case_summary", [])} for r in (before, after)]
    compatible = not answer["evaluator_changed"] and before["mode"] == after["mode"]
    for row in answer["results"]:
        old, new = row["before"], row["after"]
        if not old or not new:
            continue
        if not compatible:
            row["outcome"] = "not_comparable"
        a, b = old.get("observation"), new.get("observation")
        row["structured_output_changed"] = a["output_fingerprint"] != b["output_fingerprint"] if a and b else None
        row["response_text_changed"] = a.get("response_fingerprint") != b.get("response_fingerprint") if a and b else None
        row["check_changes"] = []
        checks = [{c["check_id"]: c for c in run["checks"]} for run in (old, new)]
        for key in sorted(set(checks[0]) | set(checks[1])):
            x, y = [c.get(key) for c in checks]
            if x != y:
                row["check_changes"].append({"check_id": key, "before": x, "after": y})
        row["metric_deltas"] = {}
        if a and b:
            for key in ("model_calls", "duration_seconds"):
                row["metric_deltas"][key] = b[key] - a[key] if a[key] is not None and b[key] is not None else None
            for key in ("input_token_count", "output_token_count", "total_token_count"):
                x, y = a["usage"].get(key), b["usage"].get(key)
                row["metric_deltas"][key] = y - x if x is not None and y is not None else None
    answer["interpretation"] = "Matched repetitions are observations, not matched random seeds. Prompt/skill edits can be compared; code, data, model or settings changes are confounders. Inspect changed paths and coverage before attributing improvements."
