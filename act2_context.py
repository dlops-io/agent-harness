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


# %% Two independent sessions/inventories; each mode runs once, without an evaluation suite.
async def run_act2(recorder, *, scenario="standard", mode="both", model=MODEL,
                   progress=None, on_result=None):
    from act1_agent import run_agent
    if mode not in {"basic", "enriched", "both"}:
        raise ValueError("Choose basic, enriched, or both context modes.")
    modes = ["basic", "enriched"] if mode == "both" else [mode]
    results = []
    for current in modes:
        if progress:
            progress(f"Starting {current} context with {model}")
        result = await run_agent(recorder, scenario=scenario, mode=current, model=model, progress=progress, act=2)
        results.append(result)
        if on_result:
            on_result(result)
    return results
