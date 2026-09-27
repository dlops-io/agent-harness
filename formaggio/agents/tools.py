"""The same five read-only/proposal tools are available in both context modes."""
from agent_framework import tool

from formaggio.shop.data_models import LineItem


def build_tools(store, request, recorder, run_id):
    @tool
    def get_catalog() -> dict:
        """Read all fictional shop products, current stock, prices, and allergens."""
        return {"products": [{**p.model_dump(mode="json"), "stock_g": store.inventory[p.product_id]}
                              for p in store.products.values()]}

    @tool
    def check_stock(items: list[LineItem]) -> dict:
        """Check product identity and aggregated quantities against current stock."""
        report = store.validate(request, items)
        issues = [v.model_dump() for v in report.violations
                  if v.rule in {"unknown_product", "stock", "increment", "minimum_quantity"}]
        return {"ok": not issues, "violations": issues, "items": [i.model_dump() for i in report.items],
                "scope": "Stock and identity only; not a complete order validation."}

    @tool
    def price_order(items: list[LineItem]) -> dict:
        """Calculate exact cheese-only cents, excluding tax, shipping, and pairings."""
        report = store.validate(request, items)
        if any(v.rule == "unknown_product" for v in report.violations):
            return {"error": "Cannot quote a cart with unknown products."}
        return {"items": [i.model_dump() for i in report.items], "line_totals_cents": report.line_totals_cents,
                "subtotal_cents": report.subtotal_cents, "needs_manager_approval": report.needs_manager_approval,
                "scope": "Quote only; not stock reservation, validation, or approval."}

    @tool
    def get_pairings(items: list[LineItem]) -> dict:
        """Read compatible, allergy-filtered pairing suggestions for a valid cart."""
        report = store.validate(request, items)
        if not report.ok:
            return {"error": "Pairings require a valid cart.", "violations": [v.model_dump() for v in report.violations]}
        return {key: [p.model_dump(mode="json") for p in value]
                for key, value in store.eligible_pairings(request, items).items()}

    @tool
    def preview_order(items: list[LineItem]) -> dict:
        """Record an unvalidated proposal only. Never place, approve, or reserve an order."""
        result = {"status": "unvalidated_proposal", "order_placed": False,
                  "items": [LineItem.model_validate(i).model_dump() for i in items]}
        if recorder is not None:
            recorder.event(run_id, "proposal.submitted", result)
        return result

    return [get_catalog, check_stock, price_order, get_pairings, preview_order]
