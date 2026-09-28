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


def print_context_comparison(results):
    print("\n📊 Comparison — quality first; call counts alone do not show improvement")
    for result in results:
        evidence = result.get("context_evidence", {})
        match = evidence.get("mild_match")
        mild = "yes" if match is True else "no" if match is False else "unmeasured"
        status = ", ".join(c["status"] for c in evidence.get("checks", [])) or "unmeasured"
        print(f"  {result['mode']}: valid cart = {result['cart_check_status']}; all cheeses mild (funk 0–2) = {mild}; context check = {status}")
    print("  Basic is not graded on preferences it never received. Matching mild cheeses alone does not prove retrieval helped.")


def print_shipping_demo():
    print("\n🛡️ Harness policy check — fixed counterexample, NOT the agent's cart")
    print("  Same cart: Comté 350 g + Bûcheron 350 g + Époisses 300 g. Change only the destination.")
    for state, report in shipping_policy_demo().items():
        print(f"  {state}: {'PASS' if report.ok else 'REJECTED'}")
        for violation in report.violations:
            print(f"    {violation.rule}: {violation.detail}")
    print("  CartCheck reports this violation in Acts 1–2. Act 3's workflow and checkout enforce it before placing an order.")
    print("  This models store/regulatory policy enforcement with a FICTIONAL classroom PA rule, not actual legal advice. No order placed by this check.")
