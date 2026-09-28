"""Explain actual workflow proposals without changing model inputs or decisions."""


def proposal_details(store, items, previous=()):
    def collect(lines):
        result = {}
        for item in lines:
            product = store.resolve(item.product)
            key = product.product_id if product else item.product
            if key not in result:
                result[key] = {"product": key, "name": product.name if product else item.product,
                               "grams": 0, "raw_milk": product.raw_milk if product else None}
            result[key]["grams"] += item.grams
        return result
    current, prior = collect(items), collect(previous)
    return {"items": list(current.values()),
            "removed": [p for key, p in prior.items() if key not in current],
            "added": [p for key, p in current.items() if key not in prior],
            "quantity_changes": [{"product": key, "before_grams": prior[key]["grams"], "after_grams": p["grams"]}
                                 for key, p in current.items() if key in prior and prior[key]["grams"] != p["grams"]]}


def proposal_lines(details):
    def label(item):
        return f"{item['name']} ({item['product']}) — {item['grams']} g"
    lines = [f"🧺 Proposed cart — attempt {details['attempt']}:"]
    for item in details["items"]:
        milk = "unknown product" if item["raw_milk"] is None else "RAW MILK" if item["raw_milk"] else "not raw milk"
        lines.append(f"  {label(item)} · {milk}")
    if not details["items"]:
        lines.append("  Empty cart")
    if details["attempt"] > 1:
        lines.append("🔄 Changes from the previous proposal:")
        lines.extend("  Removed: " + label(p) for p in details["removed"])
        lines.extend("  Added: " + label(p) for p in details["added"])
        lines.extend(f"  Quantity: {p['product']} {p['before_grams']} g → {p['after_grams']} g" for p in details["quantity_changes"])
        if not any(details[key] for key in ("removed", "added", "quantity_changes")):
            lines.append("  No changes; the same cart was proposed again.")
    return lines
