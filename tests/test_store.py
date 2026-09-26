import copy
import unittest

from pydantic import ValidationError

from formaggio.config import load_json
from formaggio.shop.data_models import LineItem, Request
from formaggio.shop.store import Store
from tests.support import fixture


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()

    def rules(self, name, **changes):
        request, items = fixture(name, **changes)
        return {v.rule for v in self.store.validate(request, items).violations}

    def test_standard_known_receipt(self):
        request, items = fixture()
        report = self.store.validate(request, items)
        self.assertTrue(report.ok)
        self.assertEqual(report.total_grams, 1000)
        self.assertEqual(report.subtotal_cents, 5010)
        self.assertEqual(report.line_totals_cents, (1435, 2070, 1505))
        self.assertFalse(report.needs_manager_approval)

    def test_large_order_known_receipt(self):
        request, items = fixture("manager-approval")
        report = self.store.validate(request, items)
        self.assertTrue(report.ok)
        self.assertEqual((report.total_grams, report.subtotal_cents), (4800, 22080))
        self.assertTrue(report.needs_manager_approval)

    def test_alias_cannot_bypass_shipping(self):
        request, items = fixture()
        items[0] = LineItem(product="  COMTE 18 mois ", grams=300)
        report = self.store.validate(request, items)
        self.assertIn("shipping", {v.rule for v in report.violations})
        self.assertIn("comte", {i.product for i in report.items})

    def test_duplicate_stock_aggregates_aliases(self):
        request, items = fixture("duplicate-stock")
        report = self.store.validate(request, items)
        stock = [v for v in report.violations if v.rule == "stock"]
        self.assertEqual(len(stock), 1)
        self.assertEqual(stock[0].product_id, "epoisses")
        self.assertIn("1200", stock[0].detail)

    def test_rule_counterexamples(self):
        cases = {"unknown-product": "unknown_product", "out-of-stock": "stock", "specific-nut": "allergen",
                 "dairy-allergy": "allergen", "budget": "budget", "under-sized": "portions", "complaint": "complaint"}
        for scenario, rule in cases.items():
            with self.subTest(scenario=scenario):
                self.assertIn(rule, self.rules(scenario))

    def test_missing_and_unknown_allergies_are_not_none(self):
        self.assertTrue({"allergy_confirmation", "destination"} <= self.rules("missing-details"))
        self.assertIn("unknown_allergy", self.rules("standard", allergies=["sesame"]))
        self.assertNotIn("allergy_confirmation", self.rules("standard", allergies=[]))

    def test_explicit_requirements_survive(self):
        self.assertIn("country_preference", self.rules("standard", required_countries=["Spain"]))
        request, _ = fixture()
        mild = [LineItem(product=p, grams=g) for p, g in [("bucheron",350),("gouda",350),("halloumi",300)]]
        self.assertIn("funk_preference", {v.rule for v in self.store.validate(request,mild).violations})

    def test_country_and_funk_requirements_apply_to_menu_not_every_cheese(self):
        request, items = fixture()  # Bûcheron, Époisses and Italian Taleggio.
        self.assertTrue(self.store.validate(request, items).ok)
        self.assertTrue(self.store.validate(request.model_copy(update={
            "required_countries": ("France", "Italy")}), items).ok)
        rules = {v.rule for v in self.store.validate(request.model_copy(update={
            "required_countries": ("France", "Spain")}), items).violations}
        self.assertIn("country_preference", rules)

    def test_catalog_allergy_explanation_requires_confirmed_complete_exclusion(self):
        request, _ = fixture("dairy-allergy")
        self.assertIn("milk", self.store.catalog_allergy_conflict(request))
        for changes in ({"allergies_confirmed": False}, {"allergies": None},
                        {"allergies": ()}, {"allergies": ("nuts",)},
                        {"allergies": ("unknown-allergen",)}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.store.catalog_allergy_conflict(request.model_copy(update=changes)))

    def test_quantity_and_request_types(self):
        for grams in (0, -50, 100.5, "100", True):
            with self.subTest(grams=grams), self.assertRaises(ValidationError):
                LineItem(product="gouda", grams=grams)
        for party in (0, -1, "12", True):
            with self.subTest(party=party), self.assertRaises(ValidationError):
                Request(customer_id="a", party_size=party)
        request, items = fixture()
        items[0] = LineItem(product="epoisses", grams=49)
        rules = {v.rule for v in self.store.validate(request, items).violations}
        self.assertTrue({"increment", "minimum_quantity", "portions"} <= rules)

    def test_half_up_per_line_not_binary_float(self):
        products = copy.deepcopy(load_json("catalog.json"))
        for product in products:
            if product["product_id"] in {"gouda", "halloumi", "bucheron"}:
                product["cents_per_100g"] = 101
        store = Store(products=products)
        request, _ = fixture(party_size=5, state="NY", allergies=[], required_countries=[], required_min_funk=None)
        report = store.validate(request,[LineItem(product=p,grams=150) for p in ["gouda","halloumi","bucheron"]])
        self.assertTrue(report.ok)
        self.assertEqual(report.line_totals_cents,(152,152,152))
        self.assertEqual(report.subtotal_cents,456)

    def test_manager_threshold_is_strict_and_cannot_override_budget(self):
        request, items = fixture("manager-approval", budget_cents=20000)
        self.assertIn("budget", {v.rule for v in self.store.validate(request,items).violations})
        # Boundary tested using a policy at the independently known cart subtotal.
        for threshold, expected in [(22080,False),(22079,True)]:
            policy = {**load_json("policies.json"),"manager_threshold_cents":threshold}
            self.assertEqual(Store(policy=policy).validate(request,items).needs_manager_approval,expected)

    def test_pairings_filter_structured_allergens(self):
        request, items = fixture(state="NY", required_min_funk=None)
        items[0] = LineItem(product="comte", grams=300)
        pairings = self.store.eligible_pairings(request,items)
        self.assertNotIn("walnut_bread", {p.pairing_id for p in pairings["comte"]})
        self.assertIn("apple", {p.pairing_id for p in pairings["comte"]})
        self.assertNotIn("epoisses",pairings)
        no_pairings = Request.model_validate({**request.model_dump(),"wants_pairings":False})
        self.assertEqual(self.store.eligible_pairings(no_pairings,items),{})
        bad_request, bad_items = fixture("out-of-stock")
        with self.assertRaises(ValueError):
            self.store.eligible_pairings(bad_request,bad_items)

    def test_catalog_rejects_ambiguous_identity_and_bad_allergens(self):
        mutations = [lambda rows: rows.append(rows[0]),
                     lambda rows: rows[1]["aliases"].append("brie"),
                     lambda rows: rows[0].update(allergens=[]),
                     lambda rows: rows[0].update(allergens=["milk","unrecognized"]),
                     lambda rows: rows[-2].update(allergens=["milk","pistachio"])]
        for mutate in mutations:
            rows=copy.deepcopy(load_json("catalog.json")); mutate(rows)
            with self.assertRaises(ValueError):
                Store(products=rows)

    def test_inventory_is_fresh_and_validation_read_only(self):
        request, items = fixture()
        before = self.store.inventory.copy()
        self.store.validate(request,items)
        self.assertEqual(self.store.inventory,before)
        self.store.inventory["epoisses"] = 0
        self.assertEqual(Store().inventory["epoisses"],900)
        self.assertIn("stock",self.rules("standard"))

    def test_all_dataset_references(self):
        scenarios=load_json("scenarios.json")
        self.assertEqual(len(scenarios),len({s["id"] for s in scenarios}))
        customers=load_json("customers.json")
        self.assertEqual(len(customers),len({c["customer_id"] for c in customers}))
        for scenario in scenarios:
            request=Request.model_validate(scenario["request"])
            self.assertIn(request.customer_id,{c["customer_id"] for c in customers})
