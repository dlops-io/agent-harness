"""Deterministic catalog rules; the checkout service owns protected placement."""
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
import unicodedata

from formaggio.config import load_json
from formaggio.shop.data_models import CartReport, LineItem, Pairing, Policy, Product, Request, Violation


def name_key(value: str) -> str:
    return " ".join("".join(c for c in unicodedata.normalize("NFKD", value.casefold())
                           if not unicodedata.combining(c)).split())


class Store:
    """Each instance owns fresh inventory; rule evaluation never changes it."""

    def __init__(self, products=None, pairings=None, policy=None):
        self.policy = Policy.model_validate(policy if policy is not None else load_json("policies.json"))
        rows = products if products is not None else load_json("catalog.json")
        self.products: dict[str, Product] = {}
        self.aliases: dict[str, str] = {}
        for row in rows:
            product = Product.model_validate(row)
            if product.product_id in self.products:
                raise ValueError(f"duplicate product ID: {product.product_id}")
            self._check_allergens(product.allergens)
            if "milk" not in product.allergens:
                raise ValueError("dairy products must declare milk")
            self.products[product.product_id] = product
            for alias in (product.product_id, product.name, *product.aliases):
                key = name_key(alias)
                if not key or (key in self.aliases and self.aliases[key] != product.product_id):
                    raise ValueError(f"empty or ambiguous product alias: {alias}")
                self.aliases[key] = product.product_id
        self.pairings: dict[str, Pairing] = {}
        styles = {p.style for p in self.products.values()}
        for row in pairings if pairings is not None else load_json("pairings.json"):
            pairing = Pairing.model_validate(row)
            self._check_allergens(pairing.allergens)
            if pairing.pairing_id in self.pairings or not pairing.styles or not set(pairing.styles) <= styles:
                raise ValueError("duplicate pairing ID or invalid pairing styles")
            self.pairings[pairing.pairing_id] = pairing
        self.inventory = {p.product_id: p.stock_g for p in self.products.values()}

    def _check_allergens(self, tags):
        if not set(tags) <= set(self.policy.known_allergens):
            raise ValueError("unknown allergen tag in catalog/pairing")
        if set(tags) & {"walnut", "pistachio", "almond"} and "tree_nuts" not in tags:
            raise ValueError("specific tree nuts must also declare tree_nuts")

    def resolve(self, name: str) -> Product | None:
        product_id = self.aliases.get(name_key(name))
        return self.products.get(product_id) if product_id else None

    def allergy_tags(self, request: Request) -> tuple[set[str], list[Violation]]:
        tags, issues = set(), []
        if request.allergies is None or not request.allergies_confirmed:
            issues.append(Violation(rule="allergy_confirmation", detail="Confirm allergies or explicitly confirm none."))
        for term in request.allergies or ():
            expansion = self.policy.allergen_aliases.get(name_key(term))
            if expansion is None:
                issues.append(Violation(rule="unknown_allergy", detail=f"Clarify allergy: {term}"))
            else:
                tags.update(expansion)
        return tags, issues

    def catalog_allergy_conflict(self, request: Request) -> str | None:
        """Explain only a proven whole-catalog allergy conflict; not general feasibility."""
        tags, issues = self.allergy_tags(request)
        if issues or not tags or not self.products:
            return None
        if all(tags.intersection(p.allergens) for p in self.products.values()):
            return ("Every cheese in the shop catalog lists an allergen that conflicts with "
                    "the confirmed allergies (" + ", ".join(request.allergies) + "). "
                    "This catalog cannot supply a compatible cheese menu. "
                    "The confirmed allergies remain fixed; no order has been placed.")
        return None

    def request_violations(self, request: Request) -> list[Violation]:
        """Check confirmed inputs without inventing a cart or asking the model."""
        _, issues = self.allergy_tags(request)
        def fail(rule, detail):
            issues.append(Violation(rule=rule, detail=detail))
        if request.party_size is None:
            fail("party_size", "Confirm number of guests.")
        if request.budget_cents is None:
            fail("budget_required", "Confirm cheese-only budget.")
        state = (request.state or "").strip().upper()
        if state not in self.policy.recognized_states:
            fail("destination", "Confirm a recognized US state or DC code.")
        if request.intent == "complaint":
            fail("complaint", "Escalate complaint; no order may be placed.")
        return issues

    def validate(self, request: Request, items: list[LineItem]) -> CartReport:
        tags, _ = self.allergy_tags(request)
        issues = self.request_violations(request)
        state = (request.state or "").strip().upper()

        def fail(rule, detail, product_id=None):
            issues.append(Violation(rule=rule, detail=detail, product_id=product_id))

        quantities: dict[str, int] = defaultdict(int)
        for raw in items:
            item = LineItem.model_validate(raw)
            product = self.resolve(item.product)
            if product is None:
                fail("unknown_product", f"Unknown product: {item.product}")
                continue
            quantities[product.product_id] += item.grams
            if item.grams % self.policy.quantity_increment_g:
                fail("increment", f"Use {self.policy.quantity_increment_g} g increments.", product.product_id)

        normalized, totals = [], []
        for product_id, grams in sorted(quantities.items()):
            product = self.products[product_id]
            normalized.append(LineItem(product=product_id, grams=grams))
            cents = (Decimal(product.cents_per_100g) * grams / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            totals.append(int(cents))
            if grams < self.policy.min_product_grams:
                fail("minimum_quantity", f"Minimum {self.policy.min_product_grams} g per product.", product_id)
            if grams > self.inventory[product_id]:
                fail("stock", f"Requested {grams} g; available {self.inventory[product_id]} g.", product_id)
            if product.raw_milk and state in self.policy.raw_milk_blocked_states:
                fail("shipping", f"Fictional shop policy blocks raw-milk shipping to {state}.", product_id)
            if tags.intersection(product.allergens):
                fail("allergen", "Conflicts with confirmed allergy.", product_id)

        total_grams, subtotal = sum(quantities.values()), sum(totals)
        if not self.policy.min_products <= len(quantities) <= self.policy.max_products:
            fail("variety", f"Choose {self.policy.min_products}–{self.policy.max_products} distinct cheeses.")
        if request.party_size and not (request.party_size * self.policy.min_grams_per_guest <= total_grams
                                       <= request.party_size * self.policy.max_grams_per_guest):
            fail("portions", "Total does not satisfy the tasting portion range.")
        if request.budget_cents is not None and subtotal > request.budget_cents:
            fail("budget", "Cheese subtotal exceeds the customer's hard budget.")
        chosen = [self.products[k] for k in quantities]
        for country in request.required_countries:
            if not any(name_key(p.country) == name_key(country) for p in chosen):
                fail("country_preference", f"Must include a cheese from {country}.")
        if request.required_min_funk is not None and not any(p.funk >= request.required_min_funk for p in chosen):
            fail("funk_preference", "Required funk level is missing.")
        return CartReport(items=tuple(normalized), line_totals_cents=tuple(totals), subtotal_cents=subtotal,
                          total_grams=total_grams, needs_manager_approval=subtotal > self.policy.manager_threshold_cents,
                          violations=tuple(issues))

    def eligible_pairings(self, request: Request, items: list[LineItem], *, allow_stock_shortage=False) -> dict[str, list[Pairing]]:
        """Revalidate the actual cart; never generate pairings for a removed proposal."""
        report = self.validate(request, items)
        if any(v.rule != "stock" or not allow_stock_shortage for v in report.violations):
            raise ValueError("Pairings require a valid cart.")
        if not request.wants_pairings:
            return {}
        tags, _ = self.allergy_tags(request)
        return {item.product: [p for p in self.pairings.values()
                              if self.products[item.product].style in p.styles and not tags.intersection(p.allergens)]
                for item in report.items}
