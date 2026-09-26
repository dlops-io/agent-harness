"""Small immutable contracts. Missing request details are allowed until validation."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Product(Record):
    product_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    aliases: tuple[str, ...] = ()
    country: str
    milk: Literal["cow", "goat", "sheep"]
    raw_milk: bool
    style: str
    funk: int = Field(strict=True, ge=0, le=5)
    cents_per_100g: int = Field(strict=True, gt=0)
    stock_g: int = Field(strict=True, ge=0)
    allergens: tuple[str, ...]


class Pairing(Record):
    pairing_id: str
    name: str
    styles: tuple[str, ...]
    kind: Literal["food", "drink"]
    ingredients: tuple[str, ...]
    allergens: tuple[str, ...]


class Policy(Record):
    version: str
    description: str
    raw_milk_blocked_states: tuple[str, ...]
    recognized_states: tuple[str, ...]
    manager_threshold_cents: int = Field(strict=True, ge=0)
    min_product_grams: int = Field(strict=True, gt=0)
    quantity_increment_g: int = Field(strict=True, gt=0)
    min_grams_per_guest: int = Field(strict=True, gt=0)
    max_grams_per_guest: int = Field(strict=True, gt=0)
    min_products: int = Field(strict=True, gt=0)
    max_products: int = Field(strict=True, gt=0)
    max_revisions: int = Field(strict=True, ge=0)
    allergen_aliases: dict[str, tuple[str, ...]]
    known_allergens: tuple[str, ...]
    allowed_vendor_recipients: tuple[str, ...]

    @model_validator(mode="after")
    def ordered_limits(self):
        if self.max_grams_per_guest < self.min_grams_per_guest or self.max_products < self.min_products:
            raise ValueError("maximum must not be below minimum")
        if not set(self.raw_milk_blocked_states) <= set(self.recognized_states):
            raise ValueError("shipping policy contains unrecognized states")
        if any(not set(tags) <= set(self.known_allergens) for tags in self.allergen_aliases.values()):
            raise ValueError("allergy alias uses an unknown tag")
        return self


class Request(Record):
    customer_id: str = Field(min_length=1)
    intent: Literal["order", "recommendation", "complaint"] = "order"
    party_size: int | None = Field(default=None, strict=True, gt=0)
    budget_cents: int | None = Field(default=None, strict=True, gt=0)
    state: str | None = None
    allergies: tuple[str, ...] | None = Field(default=None, description=(
        "Confirmed allergies are fixed exclusions: never suggest removing or relaxing them. "
        "None means unknown; an empty list explicitly means no allergies."))
    allergies_confirmed: bool = False
    order_authorized: bool = False
    required_countries: tuple[str, ...] = Field(default=(), description=(
        "Include at least one cheese from EACH listed country. Other countries are allowed; "
        "this is not an origin restriction on every cheese."))
    required_min_funk: int | None = Field(default=None, strict=True, ge=0, le=5, description=(
        "At least ONE cheese in the menu must meet or exceed this funk level. "
        "Milder cheeses may complete the menu."))
    preferences: tuple[str, ...] = ()
    wants_pairings: bool = False


class LineItem(Record):
    product: str = Field(min_length=1)  # ID or supported name/alias on input
    grams: int = Field(strict=True, gt=0)


class Violation(Record):
    rule: str
    detail: str
    product_id: str | None = None


class CartReport(Record):
    items: tuple[LineItem, ...]
    line_totals_cents: tuple[int, ...]
    subtotal_cents: int
    total_grams: int
    needs_manager_approval: bool
    violations: tuple[Violation, ...]

    @property
    def ok(self) -> bool:
        return not self.violations


class Decision(Record):
    policy_id: str
    policy_version: str
    boundary: str
    outcome: Literal["allow", "block", "require_approval"]
    reason: str


class CheckResult(Record):
    check_id: str
    expected: object
    observed: object
    status: Literal["pass", "fail", "error", "not_applicable"]
    explanation: str


class AgentReply(Record):
    """Visible proposal and customer message; never an order receipt."""
    message: str
    items: list[LineItem]


class CartProposal(Record):
    """The workflow model can propose quantities, never approve or place orders."""
    items: list[LineItem]


class PairingSelection(Record):
    product: str
    suggestions: tuple[Pairing, ...]


class ApprovalTicket(Record):
    ticket_id: str
    checkout_key: str
    fingerprint: str
    request: Request
    report: CartReport
    decision: Literal["pending", "approve", "decline"] = "pending"


class OrderReceipt(Record):
    order_id: str
    checkout_key: str
    fingerprint: str
    request: Request
    report: CartReport
    pairings: tuple[PairingSelection, ...]
    approval_ticket_id: str | None = None


class WorkflowState(Record):
    request: Request
    checkout_key: str
    attempts: int = 0
    items: tuple[LineItem, ...] = ()
    report: CartReport | None = None
    pairings: tuple[PairingSelection, ...] = ()
    ticket_id: str | None = None


class WorkflowOutcome(Record):
    status: Literal["placed", "clarification", "escalated", "unresolved", "declined", "blocked", "recommendation"]
    message: str
    attempts: int = 0
    report: CartReport | None = None
    pairings: tuple[PairingSelection, ...] = ()
    receipt: OrderReceipt | None = None


class VendorDraft(Record):
    draft_id: str
    recipient: str
    subject: str
    body: str
    fingerprint: str


class EmailReview(Record):
    ticket_id: str
    draft: VendorDraft
