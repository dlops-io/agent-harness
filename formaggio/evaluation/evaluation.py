"""Foundation evaluations plus shared experiment reports and version comparisons."""
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
import platform

from formaggio.config import DATA_DIR, ROOT, load_json, source_hashes
from formaggio.operations.governance import ApprovalRequired, Governance, PolicyBlocked, PolicyCheckError
from formaggio.shop.data_models import CheckResult, LineItem, Request
from formaggio.operations.observability import canonical
from formaggio.shop.store import Store


def read_proposals():
    return json.loads((ROOT / "tests/fixtures/proposals.json").read_text(encoding="utf-8"))


def snapshot(prompt="Foundation uses fixed proposals; no model is called."):
    """Explicit file selection excludes environment files, outputs, and credentials."""
    source = source_hashes()
    data = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in sorted(DATA_DIR.glob("*.json"))}
    skills = {str(p.relative_to(ROOT)): p.read_text(encoding="utf-8")
              for p in sorted((ROOT / "skills").rglob("*")) if p.is_file()}
    return {"prompt": prompt, "source_hashes": source, "data": data, "skills": skills,
            "fixtures": read_proposals(), "python": platform.python_version(),
            "dependencies": {name: version(name) for name in ("agent-framework", "pydantic", "openai", "opentelemetry-sdk")},
            "model": None, "mode": "fixture"}


def assess(case, report):
    """Check expected properties independently of any final response text."""
    actual_rules = sorted({v.rule for v in report.violations})
    observations = [
        ("validity", case["expected_valid"], report.ok, report.ok == case["expected_valid"]),
        ("required_violations", case["required_violation_rules"], actual_rules,
         set(case["required_violation_rules"]).issubset(actual_rules)),
        ("manager_approval", case["expected_manager_approval"], report.needs_manager_approval,
         report.needs_manager_approval == case["expected_manager_approval"]),
    ]
    if case.get("expected_subtotal_cents") is not None:
        expected = case["expected_subtotal_cents"]
        observations.append(("subtotal", expected, report.subtotal_cents, expected == report.subtotal_cents))
    return [CheckResult(check_id=name, expected=expected, observed=observed,
                        status="pass" if ok else "fail", explanation="Expectation met." if ok else "Observed property differs.")
            for name, expected, observed, ok in observations]


def run_suite(recorder, label, *, repeats=1, prompt=None, cases=None, propose=None):
    """A fresh Store per repetition; no hidden model calls or persisted business state.

    propose is an optional trusted test callback: (case, scenario, store) -> items.
    It is fixture-only in Step 1 and must not be used to label live calls as fixtures.
    """
    selected = load_json("evaluation_cases.json") if cases is None else cases
    if not selected or len({c["case_id"] for c in selected}) != len(selected):
        raise ValueError("Provide nonempty, uniquely named cases.")
    scenarios = {s["id"]: s for s in load_json("scenarios.json")}
    fixtures = read_proposals()
    # Reject broken case references before creating a partial experiment.
    for case in selected:
        if 0 not in case["acts"] or case["scenario_id"] not in scenarios or case["fixture_id"] not in fixtures:
            raise ValueError("Case is not a runnable foundation fixture.")
    version_id = recorder.version(snapshot() if prompt is None else snapshot(prompt))
    evaluator_version = sha256((Path(__file__).read_bytes() + canonical(selected).encode())).hexdigest()
    experiment_id = recorder.experiment(label, version_id, evaluator_version,
                                        [c["case_id"] for c in selected], repeats)
    for case in selected:
        for repetition in range(1, repeats + 1):
            run_id = recorder.start_run(version_id, case_id=case["case_id"], repetition=repetition,
                                        experiment_id=experiment_id)
            try:
                with recorder.span(run_id, "foundation.evaluate", "evaluation"):
                    store = Store()
                    scenario = scenarios[case["scenario_id"]]
                    request = Request.model_validate(scenario["request"])
                    raw = propose(case, scenario, store) if propose else fixtures[case["fixture_id"]]
                    items = [LineItem.model_validate(item) for item in raw]
                    recorder.event(run_id, "fixture.loaded", {"case_id": case["case_id"], "request": request.model_dump(),
                                                               "proposal": [i.model_dump() for i in items]})
                    with recorder.span(run_id, "store.validate", "validation"):
                        report = store.validate(request, items)
                        recorder.event(run_id, "cart.validated", {"ok": report.ok, **report.model_dump()})
                    gate = Governance(recorder, run_id, store.policy.version)
                    try:
                        gate.execute("checkout.preview", {"preview_only": True},
                                     lambda: gate.checkout(request, report),
                                     lambda: {"eligible": True, "order_placed": False})
                        checkout_outcome = "allow"
                    except ApprovalRequired:
                        checkout_outcome = "require_approval"
                    except PolicyCheckError:
                        raise
                    except PolicyBlocked:
                        checkout_outcome = "block"
                    for check in assess(case, report):
                        recorder.evaluation(run_id, check, evaluator_version)
                    expected_outcome = case["expected_checkout"]
                    recorder.evaluation(run_id, CheckResult(check_id="checkout_policy", expected=expected_outcome,
                        observed=checkout_outcome, status="pass" if expected_outcome == checkout_outcome else "fail",
                        explanation="Preview only; no order or manager decision was created."), evaluator_version)
                recorder.finish_run(run_id)
            except Exception as exc:
                recorder.evaluation(run_id, CheckResult(check_id="execution", expected="completed", observed=type(exc).__name__,
                                                        status="error", explanation=str(exc)), evaluator_version)
                recorder.finish_run(run_id, "error", str(exc))
    return report_experiment(recorder, label)


def report_experiment(recorder, label):
    batches = recorder.query("SELECT * FROM experiments WHERE label=?", (label,))
    if not batches:
        raise ValueError(f"Unknown experiment: {label}")
    batch = batches[0]
    rows = recorder.query("""SELECT r.run_id,r.case_id,r.repetition,r.status AS run_status,
                             e.check_id,e.status,e.expected_json,e.observed_json,e.explanation
                             FROM runs r LEFT JOIN evaluations e ON r.run_id=e.run_id
                             WHERE r.experiment_id=? ORDER BY r.case_id,r.repetition,e.check_id""", (batch["experiment_id"],))
    runs = {}
    for row in rows:
        run = runs.setdefault(row["run_id"], {"run_id": row["run_id"], "case_id": row["case_id"], "repetition": row["repetition"],
                                             "run_status": row["run_status"], "checks": []})
        if row["check_id"] is not None:
            run["checks"].append({"check_id": row["check_id"], "status": row["status"],
                                   "expected": json.loads(row["expected_json"]), "observed": json.loads(row["observed_json"]),
                                   "explanation": row["explanation"]})
    for run in runs.values():
        run["passed"] = run["run_status"] in {"completed", "blocked"} and any(c["status"] == "pass" for c in run["checks"]) and all(c["status"] in {"pass", "not_applicable"} for c in run["checks"])
    snapshot = json.loads(recorder.query("SELECT snapshot_json FROM versions WHERE version_id=?", (batch["version_id"],))[0]["snapshot_json"])
    result = {"label": label, "version_id": batch["version_id"], "evaluator_version": batch["evaluator_version"],
            "expected_runs": len(json.loads(batch["cases_json"])) * batch["repeats"],
            "completed_records": len(runs), "passed": sum(r["passed"] for r in runs.values()),
            "runs": list(runs.values()), "mode": snapshot.get("mode", "fixture")}
    if snapshot.get("kind") == "agent_evaluation":
        from formaggio.evaluation.evaluation_reports import enrich_report
        enrich_report(recorder, result, batch, snapshot)
    return result


def compare(recorder, label_a, label_b):
    a, b = (report_experiment(recorder, label) for label in (label_a, label_b))
    maps = [{(r["case_id"], r["repetition"]): r for r in report["runs"]} for report in (a, b)]
    results = []
    for key in sorted(set(maps[0]) | set(maps[1])):
        old, new = (m.get(key) for m in maps)
        if old is None or new is None:
            outcome = "unmatched"
        elif a["evaluator_version"] != b["evaluator_version"]:
            outcome = "not_comparable"
        elif old["passed"] == new["passed"]:
            outcome = "unchanged"
        else:
            outcome = "improved" if new["passed"] else "regressed"
        results.append({"case_id": key[0], "repetition": key[1], "outcome": outcome,
                        "before": old, "after": new})
    snapshots = [json.loads(recorder.query("SELECT snapshot_json FROM versions WHERE version_id=?", (r["version_id"],))[0]["snapshot_json"])
                 for r in (a, b)]
    changed = sorted(k for k in set(snapshots[0]) | set(snapshots[1]) if snapshots[0].get(k) != snapshots[1].get(k))
    answer = {"before": label_a, "after": label_b, "changed_configuration": changed,
            "confounding_changes": [k for k in changed if k not in {"prompt", "skills", "proposer_prompt"}],
            "evaluator_changed": a["evaluator_version"] != b["evaluator_version"], "results": results}
    if any(s.get("kind") == "agent_evaluation" for s in snapshots):
        from formaggio.evaluation.evaluation_reports import enrich_comparison
        enrich_comparison(answer, a, b, snapshots)
    return answer
