"""Small, explicit Act 2 rubric; no model judge or guesses from response wording."""
from formaggio.shop.data_models import CheckResult


def context_evidence(request, reply, packet, store):
    """Score actual cheese selections, separately from validity and prose quality.

    The mild-cheese classroom rubric is funk <= 2. Saved hints are optional;
    missing/unknown products never count as a successful recommendation. Matching
    the rubric is observable behavior, not proof of the model's internal reasoning.
    """
    preferences = [preference for source in packet["sources"]
                   if source.get("trust") == "saved_preferences"
                   for preference in source["content"].get("preferences", [])]
    resolved = [store.resolve(item.product) for item in reply.items]
    products = [{"product": product.product_id, "funk": product.funk}
                for product in resolved if product is not None]
    known = bool(resolved) and all(product is not None for product in resolved)
    mild_match = all(p["funk"] <= 2 for p in products) if known else None
    override = request.required_min_funk is not None and request.required_min_funk > 2
    strong_match = (any(p["funk"] >= request.required_min_funk for p in products)
                    if known and override else None)
    has_mild = "mild cheeses" in preferences
    if has_mild and override:
        check = CheckResult(check_id="context.current_request", expected=True, observed=strong_match,
            status="pass" if strong_match else "fail",
            explanation=f"Today's request for at least one cheese at funk {request.required_min_funk}+ overrides the saved mild preference.")
    elif has_mild and not request.preferences:
        check = CheckResult(check_id="context.saved_preference", expected=True, observed=mild_match,
            status="pass" if mild_match else "fail",
            explanation="Personalization rubric: every selected cheese has funk 0–2. This is separate from cart validity.")
    else:
        check = CheckResult(check_id="context.saved_preference", expected="Applicable supplied preference",
            observed=None, status="not_applicable", explanation=(
                "No saved preferences were supplied. A matching cart could be coincidental; do not credit retrieval."
                if not preferences else
                "No automatic rubric for these preferences or this free-text override. Review the answer manually."))
    return {"preferences_supplied": preferences, "products": products, "mild_match": mild_match,
            "current_funk_match": strong_match, "checks": [check.model_dump(mode="json")],
            "manual_review": "Check the final pairing recommendations against any supplied nonalcoholic preference. Pairing-tool options are not final recommendations; prose is not automatically graded.",
            "interpretation": "A matching recommendation is evidence of appropriate output, not proof that context caused it. Compare repeated runs; ties and regressions are valid results."}
