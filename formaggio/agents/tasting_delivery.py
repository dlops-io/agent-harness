"""Validate final tasting-plan structure and render facts from authoritative menus."""
from typing import Literal

from pydantic import Field, ValidationError, create_model

from formaggio.shop.data_models import Record


class TastingCourse(Record):
    product: str
    reason: str = Field(min_length=1)
    pairing_ids: list[str]


class TastingPlan(Record):
    request_id: str
    courses: list[TastingCourse] = Field(min_length=1)
    serving_notes: str = Field(min_length=1)
    open_questions: list[str]


class TastingReply(Record):
    message: str
    plans: list[TastingPlan]


def tasting_reply_schema(store):
    """Constrain provider output to this catalog; accepted-menu checks still run locally."""
    course = create_model("TastingCourse", __base__=TastingCourse,
                          product=(Literal[tuple(store.products)], ...),
                          pairing_ids=(list[Literal[tuple(store.pairings)]], ...))
    plan = create_model("TastingPlan", __base__=TastingPlan,
                        courses=(list[course], Field(min_length=1)))
    return create_model("TastingReply", __base__=TastingReply, plans=(list[plan], ...))


DELIVERY_INSTRUCTIONS = """
Return your final answer as TastingReply. Put the concise outcome summary in
message and the actual tasting plans in plans; saying a plan is complete is not
a plan. For each required menu include every accepted product exactly once in
courses, in serving order, with a reason and tool-verified pairing_ids. Select at least one pairing
per course when suggestions are available; otherwise use an empty list. Include serving_notes and open_questions (an empty list is allowed
when there are none). The application renders confirmed quantities, prices and
listed allergens from the authoritative menu; do not invent or change them.
For the event planner use request_id "event". For composed orders use their
confirmed request IDs and provide a plan only for placed or recommendation
outcomes. While waiting for host review, plans may be empty. Read assess_event
for pairing IDs and product information before preparing a plan.
"""


def deliver_tasting_reply(text, menus, store, recorder, run_id):
    """Return readable output and structural readiness; prose quality is separate.

    menus maps request IDs to (confirmed Request, assessment, authoritative status).
    This function cannot approve, place, revise or repair an order.
    """
    errors, rendered, reply = [], [], None
    try:
        reply = TastingReply.model_validate_json(text)
    except ValidationError:
        errors.append("Final response did not contain the required tasting-plan structure.")
    if reply is not None:
        ids = [plan.request_id for plan in reply.plans]
        if len(ids) != len(set(ids)) or set(ids) != set(menus):
            errors.append("Provide exactly one tasting plan for each required menu.")
        for plan in reply.plans:
            if plan.request_id not in menus:
                continue
            request, assessment, status = menus[plan.request_id]
            report = assessment["report"]
            items = {item["product"]: item["grams"] for item in report["items"]}
            resolved = [store.resolve(course.product) for course in plan.courses]
            if any(product is None for product in resolved):
                errors.append(f"{plan.request_id}: a course names an unknown catalog product.")
                continue
            courses = [course.model_copy(update={"product": product.product_id})
                       for course, product in zip(plan.courses, resolved)]
            products = [course.product for course in courses]
            if len(products) != len(set(products)) or set(products) != set(items):
                errors.append(f"{plan.request_id}: courses must match the confirmed menu exactly.")
                continue
            pairings = {product: {p["pairing_id"]: p for p in values}
                        for product, values in assessment["conditional_pairings"].items()}
            if (not plan.serving_notes.strip() or any(not course.reason.strip() or
                    len(course.pairing_ids) != len(set(course.pairing_ids)) or
                    (pairings.get(course.product) and not course.pairing_ids) or
                    any(p not in pairings.get(course.product, {}) for p in course.pairing_ids)
                    for course in courses)):
                errors.append(f"{plan.request_id}: use serving notes and verified pairings for every course.")
                continue
            lines = [f"## Host tasting plan — {plan.request_id}", "### Confirmed brief",
                     f"{request.party_size} guests · {request.state} · Budget ${request.budget_cents / 100:.2f}.",
                     "Confirmed allergies: " + (", ".join(request.allergies or ()) or "none") + "."]
            if request.required_countries:
                lines.append("Include at least one cheese from each of: " + ", ".join(request.required_countries) + ".")
            if request.required_min_funk is not None:
                lines.append(f"Include at least one cheese at funk {request.required_min_funk} or higher.")
            lines.append("### Tasting sequence")
            for index, course in enumerate(courses, 1):
                lines.append(f"{index}. {store.products[course.product].name}: {items[course.product]} g — {course.reason}")
            lines.extend([plan.serving_notes, "### Pairing suggestions"])
            for course in courses:
                suggestions = []
                for key in course.pairing_ids:
                    pairing = pairings[course.product][key]
                    allergens = ", ".join(pairing["allergens"]) or "none listed"
                    suggestions.append(f"{pairing['name']} (listed allergens: {allergens})")
                lines.append(f"- {store.products[course.product].name}: " + ("; ".join(suggestions) or ("No compatible pairings listed." if request.wants_pairings else "Pairings not requested.")))
            lines.extend(["### Quote and availability",
                          f"{report['total_grams']} g · Cheese subtotal ${report['subtotal_cents'] / 100:.2f}.",
                          "Excludes tax, shipping and pairing costs.", f"Authoritative status: {status}."])
            if assessment.get("shortfalls"):
                lines.append("Shop stock is short; vendor availability remains unconfirmed.")
            lines.extend(["### Open questions and next actions", *(f"- {q}" for q in plan.open_questions)])
            if not plan.open_questions:
                lines.append("No additional questions proposed.")
            rendered.append("\n".join(lines))
    ready = not errors
    output = reply.message if reply is not None else text
    if ready:
        output = "\n\n".join([output, *rendered]).strip()
    else:
        output += "\n\n⚠️ Tasting plan incomplete: " + " ".join(errors)
    recorder.event(run_id, "delivery.checked", {"ready": ready, "required_menus": list(menus),
        "delivered_menus": [p.request_id for p in reply.plans] if ready else [], "errors": errors})
    return output, ready
