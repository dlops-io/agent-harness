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
            "output_tokens": total("output_token_count")}


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
