"""Run the context, workflow, harness and skills lessons or inspect their recorded steps."""
import asyncio
import argparse
import json
from pathlib import Path

from formaggio.config import MODEL, OUTPUT_DIR, load_json
from formaggio.evaluation.evaluation import compare, report_experiment, run_suite
from formaggio.operations.observability import Recorder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--act", type=int, choices=[1, 2, 3, 4, 5, 6], help="1: simple agent; 2: context; 3: workflow; 4: harness; 5: skills; 6: composition")
    action.add_argument("--preview-context", action="store_true", help="Inspect both context packets offline")
    action.add_argument("--foundation", action="store_true", help="Run fixed proposals; makes no model calls")
    action.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    action.add_argument("--report", metavar="LABEL", help="Read an existing evaluation batch without model calls")
    action.add_argument("--inspect-run", metavar="RUN_ID")
    action.add_argument("--list-runs", action="store_true")
    action.add_argument("--backup", type=Path, metavar="DESTINATION")
    parser.add_argument("--db", type=Path, default=OUTPUT_DIR / "observability.sqlite")
    parser.add_argument("--label", help="Unique experiment label, required for --foundation")
    parser.add_argument("--repeats", type=int, default=None, help="Evaluation repeats: default 5 live/agent, 1 foundation")
    parser.add_argument("--evaluate", choices=["core"], help="Run applicable scenario cases for the chosen act")
    parser.add_argument("--case", action="append", dest="case_ids", help="Limit evaluation to a named case; repeat for multiple cases")
    parser.add_argument("--prompt-file", type=Path, help="Execute alternate instructions in agent evaluations; foundation only records them")
    parser.add_argument("--proposer-prompt-file", type=Path, help="Act 6 evaluation: alternate inner cart-proposer instructions")
    parser.add_argument("--skills-dir", type=Path, help="Acts 5/6 evaluation: alternate skill folders")
    parser.add_argument("--json-output", type=Path, help="Save evaluation/report/comparison JSON")
    parser.add_argument("--scenario", default=None)
    parser.add_argument("--context", choices=["basic", "enriched", "both"], default=None)
    parser.add_argument("--model", default=MODEL, help="Model ID; defaults to OPENAI_CHAT_MODEL or formaggio/config.py")
    parser.add_argument("--show-context", action="store_true", help="Print the supplied context before live calls")
    parser.add_argument("--fixture", action="store_true", help="Acts 3–6: scripted inputs/local responses; no service calls")
    parser.add_argument("--manager-decision", choices=["approve", "decline"],
                        help="Acts 3/6: explicit TEST manager decision; otherwise ask a human when needed")
    parser.add_argument("--email-decision", choices=["approve", "decline"], help="Acts 4–5: explicit TEST mock-email decision")
    parser.add_argument("--vendor-document", choices=["clean", "malicious", "benign", "quoted"], default=None)
    parser.add_argument("--demo-compaction", action="store_true", help="Acts 4–5: seed labeled synthetic history to trigger compaction")
    parser.add_argument("--simulate-detector-miss", action="store_true", help="Acts 4–5 fixture only: miss injection, then block the prohibited recipient")
    parser.add_argument("--customer", choices=[c["customer_id"] for c in load_json("customers.json")],
                        default=None, help="Acts 4–5: customer ID; defaults to the scenario customer")
    parser.add_argument("--remember-preference", action="append", default=[], help="Acts 4–5: explicitly save a customer-confirmed preference")
    parser.add_argument("--memory-db", type=Path, help="Acts 4–5: separate operational preference database")
    args = parser.parse_args()
    args.repeats = args.repeats if args.repeats is not None else (5 if args.evaluate else 1)
    if args.evaluate and (not args.act or not args.label):
        parser.error("--evaluate requires --act and a new --label.")
    if args.evaluate and any([args.scenario, args.manager_decision, args.email_decision, args.vendor_document,
            args.demo_compaction, args.simulate_detector_miss, args.customer, args.remember_preference, args.memory_db, args.show_context]):
        parser.error("Evaluation cases define scenarios, decisions and fresh state. Use --case to select cases.")
    if (args.case_ids or args.skills_dir or args.proposer_prompt_file) and not args.evaluate:
        parser.error("--case, --skills-dir and --proposer-prompt-file require --evaluate.")
    if args.skills_dir and args.act not in {5, 6}:
        parser.error("--skills-dir applies to Acts 5/6 evaluations.")
    if args.proposer_prompt_file and args.act != 6:
        parser.error("--proposer-prompt-file applies to Act 6 evaluations.")
    if args.prompt_file and not (args.evaluate or args.foundation):
        parser.error("--prompt-file applies to --evaluate or --foundation.")
    if args.json_output and not (args.evaluate or args.report or args.compare):
        parser.error("--json-output applies to evaluation/report/comparison.")
    if args.json_output and args.json_output.resolve() == args.db.resolve():
        parser.error("JSON output must not overwrite the SQLite database.")
    args.scenario = args.scenario or ("event-shortage" if args.act in {4, 5} else "standard")
    if args.foundation and not args.label:
        parser.error("--foundation requires --label; recorded labels cannot be overwritten")
    if args.act == 1 and args.context not in {None, "basic"}:
        parser.error("Act 1 uses basic context. Use --act 2 for the comparison.")
    if args.fixture and args.act not in {3, 4, 5, 6} and not args.evaluate:
        parser.error("--fixture applies only to --act 3, 4, 5 or 6.")
    if args.manager_decision and args.act not in {3, 6}:
        parser.error("--manager-decision applies only to --act 3 or 6.")
    if any([args.email_decision, args.vendor_document, args.demo_compaction, args.simulate_detector_miss,
            args.customer, args.remember_preference, args.memory_db]) and args.act not in {4, 5}:
        parser.error("Harness options apply only to --act 4 or 5.")
    if args.simulate_detector_miss and not args.fixture:
        parser.error("--simulate-detector-miss requires --fixture.")
    if args.act in {3, 4, 5, 6} and (args.context or args.show_context):
        parser.error("Acts 3–6 assemble their own context; inspect the recorded context events.")
    if args.preview_context or (args.act and args.show_context):
        from act2_context import preview_context
        try:
            print(json.dumps(preview_context(args.scenario), ensure_ascii=False, indent=2))
        except ValueError as exc:
            parser.error(str(exc))
        if args.preview_context:
            return
    with Recorder(args.db) as recorder:
        if args.evaluate:
            from formaggio.evaluation.live_evaluation import run_live_suite
            from formaggio.agents.skill_support import SKILLS_ROOT
            try:
                report = asyncio.run(run_live_suite(recorder, args.label, act=args.act, repeats=args.repeats,
                    case_ids=args.case_ids, context=args.context, model=args.model, fixture=args.fixture,
                    prompt=args.prompt_file.read_text() if args.prompt_file else None,
                    proposer_prompt=args.proposer_prompt_file.read_text() if args.proposer_prompt_file else None,
                    skills_root=args.skills_dir or SKILLS_ROOT, progress=print))
            except Exception as exc:
                print("Evaluation stopped: " + recorder.redactor.clean(str(exc)))
                raise SystemExit(1) from None
            print_evaluation_report(report)
            export_json(args.json_output, report)
            print(f"Recorded in {args.db}. Inspect with --report {args.label} or --inspect-run RUN_ID.")
            if report["passed"] != report["expected_runs"]:
                raise SystemExit(1)
        elif args.report:
            report = report_experiment(recorder, args.report)
            print_evaluation_report(report)
            export_json(args.json_output, report)
        elif args.act:
            from act1_agent import run_act1
            from act2_context import run_act2
            if args.act == 3:
                print("Workflow lesson — " + ("FIXTURE proposals; no model calls." if args.fixture else f"model: {args.model}."))
                print("Mock orders only. Inventory and approval state last for this process; traces are saved in SQLite.")
                if args.manager_decision:
                    print(f"TEST manager response configured: {args.manager_decision}")
            elif args.act == 6:
                print("Composition lesson — " + ("SCRIPTED outer agent and workflow proposals; no service calls." if args.fixture else f"model: {args.model}."))
                print("Harness → ordering workflow → host manager review when required → authoritative result.")
                print("Mock in-memory orders only; no real purchase or email.")
                if args.manager_decision:
                    print(f"TEST manager response configured: {args.manager_decision}")
            elif args.act in {4, 5}:
                print(("Skills lesson — " if args.act == 5 else "Harness lesson — ") + ("SCRIPTED local SDK responses; no service calls." if args.fixture else f"model: {args.model}."))
                print("Sourcing plan and mock HTML email only. No order, reservation or email transmission.")
                if args.email_decision:
                    print(f"TEST email response configured: {args.email_decision}")
                if args.simulate_detector_miss:
                    print("TEST: force the detector to miss an attack; the recipient policy must still block it.")
            else:
                print(f"🧀 {'Agent and tools' if args.act == 1 else 'Context engineering'} lesson — model: {args.model}.")
                print("🛡️ Proposals only; no orders are placed.")
            try:
                if args.act == 1:
                    result = asyncio.run(run_act1(recorder, scenario=args.scenario, model=args.model, progress=print))
                    print_agent_result(result)
                elif args.act == 2:
                    asyncio.run(run_act2(recorder, scenario=args.scenario, mode=args.context or "both",
                                          model=args.model, progress=print, on_result=print_agent_result))
                elif args.act in {3, 6}:
                    from act3_workflow import run_act3
                    from act6_composition import run_act6
                    async def manager(ticket):
                        print(f"\nMANAGER REVIEW — ticket {ticket.ticket_id}")
                        print("Confirmed request:\n" + ticket.request.model_dump_json(indent=2))
                        print_cart(ticket.report)
                        if args.manager_decision:
                            return args.manager_decision
                        while True:
                            try:
                                answer = input("Approve this exact cart? Type approve or decline: ").strip().lower()
                            except EOFError:
                                raise ValueError("Manager input unavailable. Run interactively or explicitly use --manager-decision for a test.") from None
                            if answer in {"approve", "decline"}:
                                return answer
                            print("Please enter approve or decline.")
                    result = asyncio.run((run_act6 if args.act == 6 else run_act3)(recorder, scenario=args.scenario, model=args.model,
                        fixture=args.fixture, manager=manager, progress=print,
                        decision_source="test_option" if args.manager_decision else "human"))
                    (print_composition_result if args.act == 6 else print_workflow_result)(result)
                else:
                    from act4_harness import run_act4
                    from act5_skills import run_act5
                    run_lesson = run_act5 if args.act == 5 else run_act4
                    async def email_reviewer(review):
                        print(f"\nMOCK EMAIL REVIEW — ticket {review.ticket_id}")
                        print(f"To: {review.draft.recipient}\nSubject: {review.draft.subject}\n\n{review.draft.body}\n")
                        if args.email_decision:
                            return args.email_decision
                        while True:
                            try:
                                decision = input("Save this exact draft as HTML? Type approve or decline: ").strip().lower()
                            except EOFError:
                                raise ValueError("Run interactively, or use --email-decision for an explicit test response.") from None
                            if decision in {"approve", "decline"}:
                                return decision
                            print("Please enter approve or decline.")
                    result = asyncio.run(run_lesson(recorder, scenario=args.scenario, model=args.model, fixture=args.fixture,
                        document=args.vendor_document or "clean", demo_compaction=args.demo_compaction,
                        simulate_detector_miss=args.simulate_detector_miss, customer_id=args.customer,
                        remember_preferences=args.remember_preference, memory_path=args.memory_db,
                        reviewer=email_reviewer, decision_source="test_option" if args.email_decision else "human", progress=print))
                    print_harness_result(result)
            except Exception as exc:
                print("Run stopped: " + recorder.redactor.clean(str(exc)))
                print(f"Inspect the recorded error with --list-runs and --inspect-run. Database: {args.db}")
                raise SystemExit(1) from None
            print(f"💾 Recorded in {args.db}. No evaluation suite was run.")
        elif args.foundation:
            prompt = args.prompt_file.read_text(encoding="utf-8") if args.prompt_file else None
            report = run_suite(recorder, args.label, repeats=args.repeats, prompt=prompt)
            print(f"FIXTURE ONLY — {report['passed']}/{report['expected_runs']} expected runs passed; no model calls.")
            print(f"Version: {report['version_id']}\nDatabase: {args.db}")
            for run in report["runs"]:
                print(f"{'PASS' if run['passed'] else 'FAIL'} {run['case_id']} #{run['repetition']}  run={run['run_id']}")
            if report["passed"] != report["expected_runs"]:
                raise SystemExit(1)
        elif args.compare:
            report = compare(recorder, *args.compare)
            print(json.dumps(report, indent=2))
            export_json(args.json_output, report)
        elif args.inspect_run:
            rows = recorder.timeline(args.inspect_run)
            if not rows:
                parser.error("Unknown run ID")
            for row in rows:
                print(f"{row['sequence']:04d} {row['event_type']} {json.dumps(row['payload'], ensure_ascii=False)}")
        elif args.list_runs:
            print(json.dumps(recorder.query("SELECT run_id,case_id,repetition,status,mode FROM runs ORDER BY started_at"), indent=2))
        elif args.backup:
            recorder.backup(args.backup)
            print(f"Consistent backup saved: {args.backup}")


def export_json(path, value):
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"JSON saved: {path}")


def print_evaluation_report(report):
    print(f"\n{report['mode'].upper()} EVALUATION {report['label']}: {report['passed']}/{report['expected_runs']} scheduled runs passed")
    print(f"Version: {report['version_id']}")
    if "errors" in report:
        print(f"Failures: {report['failed']}; errors: {report['errors']}; missing: {len(report['missing_runs'])}.")
    for case in report.get("case_summary", []):
        print(f"{case['case_id']}: {case['passed']}/{case['scheduled']} passed; {len(case['output_variants'])} structured outcome variants; {case['distinct_tool_paths']} tool paths.")
        if case["unexercised_checks"]:
            print("  Coverage not exercised: " + ", ".join(case["unexercised_checks"]))
    for run in report["runs"]:
        print(f"{'PASS' if run['passed'] else 'FAIL/ERROR'} {run['case_id']} #{run['repetition']} — run {run['run_id']}")
        for check in run["checks"]:
            if check["status"] in {"fail", "error"}:
                print(f"  {check['check_id']}: {check['explanation']} Observed: {check['observed']}")
    if "interpretation" in report:
        print(report["interpretation"])


def print_agent_result(result):
    print(f"\n🧾 {result['mode'].upper()} CONTEXT — run {result['run_id']}")
    for source in result["context"]["sources"]:
        print(f"Loaded {source['source']}: {source['reason']}")
    excluded = result["context"]["excluded_products"]
    if excluded:
        print("Excluded from initial context: " + ", ".join(p["product_id"] for p in excluded))
    print("\n💬 Agent's response:\n" + result["reply"].message)
    report = result["report"]
    if report:
        print("\n🔍 Independent cart check (no automatic repair):")
        print(f"  {report.total_grams} g; cheese subtotal ${report.subtotal_cents / 100:.2f}")
        for item in report.items:
            print(f"  {item.product}: {item.grams} g")
        if report.ok:
            print("  ✅ Cart satisfies the shop rules.")
        else:
            for violation in report.violations:
                print(f"  ⚠️ {violation.rule}: {violation.detail}")
        if report.needs_manager_approval:
            print("  👤 Manager approval would be required in the later ordering workflow.")
    print(f"📊 Model calls: {result['model_calls']}. Tool calls: {result.get('tool_calls', 'not recorded')}.")
    print("🛡️ No order was placed.\n")


def print_cart(report):
    print(f"  {report.total_grams} g; cheese subtotal ${report.subtotal_cents / 100:.2f}")
    for item, cents in zip(report.items, report.line_totals_cents):
        print(f"  {item.product}: {item.grams} g — ${cents / 100:.2f}")
    print("  Excludes tax, shipping and pairing costs. Fictional shop policies apply.")
    for violation in report.violations:
        print(f"  ⚠️ {violation.rule}: {violation.detail}")


def print_workflow_result(result):
    outcome = result["outcome"]
    print(f"\n--- WORKFLOW {outcome.status.upper()} — run {result['run_id']} ---")
    print(outcome.message)
    if outcome.receipt:
        print(f"Mock receipt: {outcome.receipt.order_id}")
    if outcome.report:
        print_cart(outcome.report)
    for selection in outcome.pairings:
        labels = [p.name + (f" (allergens: {', '.join(p.allergens)})" if p.allergens else "")
                  for p in selection.suggestions]
        print(f"Pairings for {selection.product}: {', '.join(labels) or 'none available'}")
    print(f"Proposal attempts: {outcome.attempts}. Model calls: {result['model_calls']}.")


def print_composition_result(result):
    print(f"\n--- COMPOSITION {result['status'].upper()} — run {result['run_id']} ---")
    print("Agent's notes:\n" + result["agent_text"])
    print("\nAuthoritative workflow results:")
    from formaggio.shop.data_models import CartReport
    for order in result["orders"]:
        print(f"{order['request_id']}: {order['status']}")
        if order.get("message"):
            print(order["message"])
        if order.get("receipt"):
            print("Mock receipt: " + order["receipt"]["order_id"])
        if order.get("report"):
            print_cart(CartReport.model_validate(order["report"]))
    print("Skills loaded: " + (", ".join(result["skills_loaded"]) or "none"))
    print(f"Outer model calls: {result['model_calls']}; workflow model calls: {result['workflow_model_calls']}.")
    print("No real purchase or email. Mock orders exist only for this process.")


def print_harness_result(result):
    print(f"\n--- HARNESS {result['status'].upper()} — run {result['run_id']} ---")
    if result.get("reason"):
        print(result["reason"])
    if result.get("agent_text"):
        print("Agent's notes:\n" + result["agent_text"])
    print("\nAuthoritative application state:")
    if "skills_loaded" in result:
        print("Skills loaded: " + (", ".join(result["skills_loaded"]) or "none"))
        print("Skill resources: " + (", ".join(result["skill_resources"]) or "none"))
    for item in result.get("stock", []):
        print(f"Stock: {item['product']} — {item['stock_g']} g")
    if result.get("assessment"):
        assessment = result["assessment"]
        report = assessment["report"]
        print(f"Menu: {report['total_grams']} g; cheese quote ${report['subtotal_cents'] / 100:.2f}.")
        print(assessment["scope"])
        for item in assessment["shortfalls"]:
            print(f"Unconfirmed sourcing: {item['product']} — {item['grams']} g short.")
    for path in result["artifacts"]:
        print("HTML file: " + path)
    if not result["artifacts"]:
        print("No HTML email saved.")
    print("No order placed; no email transmitted.")
    print(f"Model calls: {result['model_calls']}. Compactions: {result.get('compactions', 0)}.")


if __name__ == "__main__":
    main()
