"""Act 2: change supplied context while holding prompt, model, and tools fixed."""
from formaggio.config import MODEL
from formaggio.agents.context import build_context, customer_message, instructions, load_scenario
from formaggio.shop.store import Store
from formaggio.agents.tools import build_tools


# %% Preview is offline: no SDK client, API key, or database writes required.
def preview_context(scenario="standard"):
    store = Store()
    request = load_scenario(scenario)
    return {"same_system_instructions": instructions(store),
            "same_customer_message": customer_message(request),
            "same_tools": [t.to_dict() for t in build_tools(store, request, None, None)],
            "basic": build_context(request, store, "basic"),
            "enriched": build_context(request, store, "enriched")}


def summarize_context_run(result, events):
    """Compare outcomes and recorded usage; absent token measurements stay unknown."""
    usage = [event["payload"].get("usage") or {} for event in events
             if event["event_type"] == "model.response"]

    def total(key):
        values = [item.get(key) for item in usage]
        if len(values) != result["model_calls"] or not values:
            return None
        if any(type(value) not in (int, float) for value in values):
            return None
        return sum(values)

    return {"mode": result["context"]["mode"],
            "cart_check": result["cart_check_status"],
            "extra_sources": len(result["context"]["sources"]),
            "excluded_products": len(result["context"]["excluded_products"]),
            "model_calls": result["model_calls"], "tool_calls": result["tool_calls"],
            "input_tokens": total("input_token_count"),
            "output_tokens": total("output_token_count"),
            "mild_match": result.get("context_evidence", {}).get("mild_match"),
            "preference_check": next((c["status"] for c in result.get("context_evidence", {}).get("checks", [])), "not_applicable")}



def build_act2(*, scenario="standard", mode="enriched", model=MODEL, execution_mode="live", prompt=None):
    """The same agent and harness, with the selected context layer."""
    from acts.act1_agent import build_act1
    return build_act1(scenario=scenario, mode=mode, model=model, act=2,
                      execution_mode=execution_mode, prompt=prompt)


# %% Two independent sessions/inventories; each mode runs once, without an evaluation suite.
async def run_act2(recorder, *, scenario="standard", mode="both", model=MODEL,
                   progress=None, on_result=None):
    if mode not in {"basic", "enriched", "both"}:
        raise ValueError("Choose basic, enriched, or both context modes.")
    modes = ["basic", "enriched"] if mode == "both" else [mode]
    results = []
    for current in modes:
        if progress:
            progress(f"Starting {current} context with {model}")
        harness = build_act2(scenario=scenario, mode=current, model=model)
        result = await harness.run(recorder, progress=progress)
        results.append(result)
        if on_result:
            on_result(result)
    return results


def shipping_policy_demo():
    """Fixed counterexample using the same validator as CartCheck and checkout.

    Separate from the model's proposal; fresh inventory, no writes or model calls.
    """
    store = Store()
    request = load_scenario("pa-shipping")
    from formaggio.config import load_json
    items = load_json("workflow_proposals.json")["pa-shipping"][0]
    return {state: store.validate(request.model_copy(update={"state": state}), items)
            for state in ("PA", "NY")}


def print_context_evidence(result):
    evidence = result.get("context_evidence")
    if not evidence:
        return
    print("\n📚 What useful information did we add, and was it used appropriately?")
    preferences = evidence["preferences_supplied"]
    print("  Saved preferences supplied: " + ("; ".join(preferences) or "none"))
    print("  Actual selections: " + (", ".join(f"{p['product']} (funk {p['funk']})" for p in evidence["products"]) or "no known products"))
    if preferences == ["French cheeses", "funky cheeses"]:
        print("  Standard is a baseline: these preferences overlap today's request. Try --scenario personalized.")
    for check in evidence["checks"]:
        print(f"  {check['status'].upper()}: {check['explanation']}")
    if "nonalcoholic pairings" in preferences:
        print("  👀 " + evidence["manual_review"])
    print("  " + evidence["interpretation"])


def context_comparison(results, events_by_run=None):
    """Compare customer outcomes for both menus, without inventing preference access."""
    events_by_run = events_by_run or {}
    rows = []
    for result in results:
        evidence = result.get("context_evidence", {})
        products = {p["product"]: p["funk"] for p in evidence.get("products", [])}
        measured = bool(products) and evidence.get("mild_match") is not None
        mild = sum(funk <= 2 for funk in products.values()) if measured else None
        checks = evidence.get("checks", [])
        rows.append({**summarize_context_run(result, events_by_run.get(result["run_id"], [])),
                     "run_id": result["run_id"], "products": products,
                     "saved_preferences": evidence.get("preferences_supplied", []),
                     "mild_count": mild, "variety_count": len(products) if measured else None,
                     "mild_percent": 100 * mild / len(products) if measured else None,
                     "preference_check_id": checks[0]["check_id"] if checks else None})
    modes = {r["mode"]: r for r in rows}
    basic, enriched = modes.get("basic"), modes.get("enriched")
    verdict = "Run both modes to compare personalization."
    if basic and enriched:
        applicable = ("mild cheeses" in enriched["saved_preferences"]
                      and enriched["preference_check_id"] == "context.saved_preference"
                      and enriched["preference_check"] != "not_applicable")
        if not applicable:
            verdict = "No mild-preference comparison applies to this request. Check today's requirements and the context-use results."
        elif basic["mild_percent"] is None or enriched["mild_percent"] is None:
            verdict = "Personalization comparison unavailable: a cart is empty, unknown, or unmeasured."
        elif any(row["cart_check"] != "passed" for row in (basic, enriched)):
            verdict = "No overall improvement claimed: both carts must pass the independent cart check. Review the failures below."
        else:
            change = enriched["mild_percent"] - basic["mild_percent"]
            if change > 0:
                verdict = f"Better personalization in this pair: enriched improved mild-cheese match by {change:g} percentage points."
            elif change < 0:
                verdict = f"Worse personalization in this pair: enriched reduced mild-cheese match by {-change:g} percentage points."
            else:
                verdict = "No personalization improvement in this pair: both menus matched the mild preference equally."
    return {"rows": rows, "verdict": verdict}


def print_context_comparison(results, events_by_run=None):
    comparison = context_comparison(results, events_by_run)
    rows = comparison["rows"]
    print("\n📊 Comparison — customer preference match")
    print("  Same customer request, model, instructions and tools; only supplied context changes.")
    print("  Mild = funk 0–2. Match counts distinct cheese varieties, not grams.")
    print("  Both menus are measured against the customer's taste; basic was not given the saved profile.")
    def number(value):
        return "unavailable" if value is None else f"{value:,}"
    def score(row):
        return ("unavailable" if row["mild_percent"] is None else
                f"{row['mild_count']}/{row['variety_count']} ({row['mild_percent']:g}%)")
    metrics = [
        ("Saved preferences supplied", lambda r: "yes" if r["saved_preferences"] else "no"),
        ("Cart check", lambda r: r["cart_check"]),
        ("Mild-cheese match", score),
        ("All cheeses mild", lambda r: "unmeasured" if r["mild_match"] is None else "yes" if r["mild_match"] else "no"),
        ("Context-use check", lambda r: r["preference_check"]),
        ("Model calls", lambda r: str(r["model_calls"])),
        ("Tool calls", lambda r: str(r["tool_calls"])),
        ("Input tokens (all calls)", lambda r: number(r["input_tokens"])),
        ("Output tokens (all calls)", lambda r: number(r["output_tokens"])),
    ]
    labels = [r["mode"].upper() for r in rows]
    values = [[str(render(r)) for r in rows] for _, render in metrics]
    widths = [max(16, len(label), *(len(v[i]) for v in values)) for i, label in enumerate(labels)]
    print("\n  " + "Measure".ljust(29) + " | " + " | ".join(v.ljust(w) for v, w in zip(labels, widths)))
    print("  " + "-" * (32 + sum(widths) + 3 * max(0, len(widths) - 1)))
    for (label, _), cells in zip(metrics, values):
        print("  " + label.ljust(29) + " | " + " | ".join(v.ljust(w) for v, w in zip(cells, widths)))
    for row in rows:
        print(f"\n  {row['mode']}: " + (", ".join(f"{p} (funk {f})" for p, f in row["products"].items()) or "no measured selections"))
        if row["saved_preferences"]:
            print("  Retrieved preferences: " + "; ".join(row["saved_preferences"]))
    print("\n  " + comparison["verdict"])
    by_mode = {r["mode"]: r for r in rows}
    if set(by_mode) == {"basic", "enriched"}:
        a, b = by_mode["basic"]["input_tokens"], by_mode["enriched"]["input_tokens"]
        if a is not None and b is not None:
            delta = b - a
            suffix = f" ({abs(delta) / a:.1%})" if a > 0 else ""
            print(f"  Input-token cost: {abs(delta):,} {'more' if delta >= 0 else 'fewer'} with enriched{suffix}.")
    print("  Pairing recommendations and factual accuracy of the prose need human review.")
    print("  One pair demonstrates these outcomes; repeat runs to measure reliability. Ties and regressions are kept.")
    return comparison



def print_shipping_demo():
    print("\n🛡️ Harness policy check — fixed counterexample, NOT the agent's cart")
    print("  Same cart: Comté 350 g + Bûcheron 350 g + Époisses 300 g. Change only the destination.")
    for state, report in shipping_policy_demo().items():
        print(f"  {state}: {'PASS' if report.ok else 'REJECTED'}")
        for violation in report.violations:
            print(f"    {violation.rule}: {violation.detail}")
    print("  CartCheck reports this violation in Acts 1–2. Act 3's workflow and checkout enforce it before placing an order.")
    print("  This models store/regulatory policy enforcement with a FICTIONAL classroom PA rule, not actual legal advice. No order placed by this check.")
