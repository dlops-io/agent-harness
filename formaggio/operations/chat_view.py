"""Read-only notebook views of recorded lessons; never an execution/approval UI.

    show_chat(DB_PATH, result["run_id"])
    show_chat(DB_PATH, result["run_id"], backstage=False)

An open Recorder is also accepted. Use the path after closing the recorder.
Model responses are evidence, not automatically customer-facing messages.
IPython is needed only by show_chat; parsing and HTML rendering work offline.
"""
from contextlib import closing
from html import escape
import json
from pathlib import Path
import re
import sqlite3

from pydantic import ValidationError

from formaggio.agents.context import customer_ask
from formaggio.operations.observability import Recorder
from formaggio.shop.data_models import Request


CSS = """
<style>
.fg-view{--ink:#172638;--muted:#526174;--line:#d6dee7;font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;color:var(--ink);background:#fff;max-width:1180px;margin:16px 0;border:1px solid var(--line);border-radius:14px;overflow:hidden;color-scheme:light}
.fg-view *{box-sizing:border-box}.fg-view header{padding:16px 20px;background:#f3f6fa;border-bottom:1px solid var(--line)}
.fg-view h2{font-size:18px;margin:0 0 8px}.fg-view h3{font-size:14px;margin:0 0 12px}.fg-view h4{font-size:14px;margin:0 0 6px}
.fg-view .fg-badges{display:flex;gap:6px;flex-wrap:wrap}.fg-view .fg-badge{padding:3px 9px;border:1px solid var(--line);border-radius:20px;background:white;font-size:12px}
.fg-view .fg-grid{display:grid;grid-template-columns:minmax(0,1fr)}
.fg-view .fg-grid.two{grid-template-columns:repeat(auto-fit,minmax(min(100%,360px),1fr))}
.fg-view .fg-pane{padding:20px;min-width:0}.fg-view .fg-back{background:#f8fafc}
.fg-view .fg-label{font-size:12px;color:var(--muted);margin:10px 0 4px}.fg-view .fg-right{text-align:right}
.fg-view .fg-bubble{padding:13px 16px;border-radius:14px;background:#edf1f6;max-width:96%;margin-bottom:16px;overflow-wrap:anywhere}
.fg-view .fg-user{margin-left:auto;background:#195fc5;color:white;border-bottom-right-radius:3px}
.fg-view .fg-body{white-space:pre-wrap;overflow-wrap:anywhere}.fg-view .fg-card{padding:12px 14px;border:1px solid var(--line);border-left:4px solid #718198;border-radius:8px;margin:12px 0;background:#fff}
.fg-view .fg-card.good{border-left-color:#1a7949}.fg-view .fg-card.bad{border-left-color:#bc3535}.fg-view .fg-card.wait{border-left-color:#b17611}
.fg-view .fg-note{font-size:12px;color:var(--muted);margin:10px 0;overflow-wrap:anywhere}
.fg-view details{border-top:1px solid var(--line);padding:10px 0}.fg-view summary{cursor:pointer;font-weight:600;overflow-wrap:anywhere}
.fg-view summary:focus-visible{outline:2px solid #195fc5;outline-offset:3px}.fg-view ul{padding-left:20px;margin:8px 0}
.fg-view li{margin:6px 0;overflow-wrap:anywhere}.fg-view pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.5 ui-monospace,monospace;max-height:360px;overflow:auto;padding:10px;background:#edf1f6}
.fg-view .fg-evidence{font-size:12px}.fg-view footer{border-top:1px solid var(--line);padding:10px 20px;color:var(--muted);font-size:12px;overflow-wrap:anywhere}
</style>
"""


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _records(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _text(value):
    return value if isinstance(value, str) else ""


def _money(value):
    return f"${value / 100:,.2f}" if type(value) in (int, float) else "quote not recorded"


def _preview(value, limit=8000):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    return text if len(text) <= limit else text[:limit] + "\n… Display shortened; full payload is in the recorded database."


def _prose(value):
    """Light formatting only: escape HTML, then support headings and bold text.

    Raw HTML, images and Markdown links are never activated by the renderer.
    """
    text = escape(str(value))
    text = re.sub(r"(?m)^#{1,6} (.+)$", r"<strong>\1</strong>", text)
    return re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", text)


def load_run(source, run_id):
    """Load a consistent recorded snapshot. Path-based reads never create a DB."""
    def read(query):
        rows = query("SELECT r.*, v.snapshot_json FROM runs r JOIN versions v "
                     "ON r.version_id=v.version_id WHERE r.run_id=?", (run_id,))
        if not rows:
            raise ValueError(f"No recorded run with ID {run_id!r}.")
        run = dict(rows[0])
        snapshot = json.loads(run.pop("snapshot_json"))
        events = query("SELECT sequence,event_id,event_type,span_id,timestamp,payload_json "
                       "FROM events WHERE run_id=? ORDER BY sequence", (run_id,))
        events = [dict(event) for event in events]
        for event in events:
            event["payload"] = json.loads(event.pop("payload_json"))
        return {"run": run, "snapshot": snapshot, "events": events}

    if isinstance(source, Recorder):
        try:
            with source.lock:
                return read(source.query)
        except sqlite3.ProgrammingError as exc:
            raise ValueError("Recorder is closed; pass its database path to show_chat instead.") from exc
    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Recorded database not found: {path}")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        return read(lambda sql, args: db.execute(sql, args).fetchall())


def _requests(snapshot, events, ask):
    if ask is not None:
        return [{"label": "Instructor-provided display text", "text": str(ask)}]
    context = _mapping(snapshot.get("context"))
    if isinstance(context.get("task"), str):
        return [{"label": "Recorded task brief", "text": context["task"]}]
    requests = []
    if snapshot.get("workflow") == "act3-as-tool":
        requests = list(context.items())
    elif snapshot.get("request"):
        requests = [("Customer request", snapshot["request"])]
    if not requests:
        brief = next((_mapping(e.get("payload")).get("request") for e in events
                      if e.get("event_type") == "request.brief"), None)
        if brief:
            requests = [("Customer request", brief)]
    result = []
    for label, request in requests:
        try:
            text = customer_ask(Request.model_validate(request))
        except ValidationError:
            text = "The recorded request could not be rendered. Inspect its recorded fields below."
        result.append({"label": f"{label} · rendered from confirmed fields", "text": text})
    return result


def _cart(report):
    report = _mapping(report)
    lines = [f"{item.get('product', '?')}: {item.get('grams', '?')} g" for item in _records(report.get("items"))]
    if "subtotal_cents" in report:
        lines.append(f"Cheese subtotal: {_money(report['subtotal_cents'])}")
    lines.extend(f"{v.get('rule', 'Check')}: {v.get('detail', '')}" for v in _records(report.get("violations")))
    return "\n".join(lines)


def _tone(status):
    if status in {"placed", "saved", "answered", "plan_proposed", "passed", "approve"}:
        return "good"
    if status in {"blocked", "error", "failed", "unresolved", "incomplete"}:
        return "bad"
    if status in {"pending_approval", "clarification", "needs_followup", "running"}:
        return "wait"
    return "info"


def build_chat(events, *, run=None, snapshot=None, ask=None):
    """Build a presentation model from evidence, without calling models or tools.

    Unknown events stay available in the evidence list. No model response is
    promoted to a customer answer; only final application result events are used.
    """
    run, snapshot = _mapping(run), _mapping(snapshot)
    events = _records(events)
    grouped = {}
    for event in events:
        grouped.setdefault(event.get("event_type", "unknown"), []).append(_mapping(event.get("payload")))

    def last(name):
        return grouped.get(name, [{}])[-1]

    cards, groups = [], {}
    workflow_requests = {p.get("workflow_id"): p.get("request_id")
                         for p in grouped.get("composition.started", []) if p.get("workflow_id")}
    ticket_requests = {p.get("ticket_id"): workflow_requests[p["checkout_key"]]
                       for p in grouped.get("approval.requested", []) if p.get("checkout_key") in workflow_requests}

    def card(title, body="", tone="info"):
        cards.append({"title": title, "body": body, "tone": tone})

    def step(group, text):
        groups.setdefault(group, []).append(text)

    final_event = next((e for e in reversed(events) if e.get("event_type") in
                       {"agent.result", "workflow.result", "harness.result", "composition.result"}), {})
    final_type, final = final_event.get("event_type"), _mapping(final_event.get("payload"))
    answer = _text(final.get("message")) if final_type == "agent.result" else (
        _text(final.get("agent_text")) if final_type in {"harness.result", "composition.result"} else "")
    if final_type == "agent.result":
        status = final.get("cart_check_status", "not recorded")
        card(f"Independent cart check: {status}", _cart(final.get("cart_report")), _tone(status))
        card("Proposal only", "No order was placed.")
    elif final_type == "workflow.result":
        card(f"Workflow outcome: {final.get('status', 'not recorded')}",
             _text(final.get("message")), _tone(final.get("status")))
        receipt = _mapping(final.get("receipt"))
        if receipt:
            card("Mock order receipt", f"Receipt: {receipt.get('order_id', '?')}\n" + _cart(receipt.get("report")), "good")
        elif final.get("report"):
            card("Checked cart", _cart(final["report"]))
    elif final_type in {"harness.result", "composition.result"}:
        card(f"Application outcome: {final.get('status', 'not recorded')}",
             _text(final.get("reason")), _tone(final.get("status")))
        if final_type == "harness.result":
            for stock in _records(final.get("stock")):
                card("Recorded stock", f"{stock.get('product', '?')}: {stock.get('stock_g', '?')} g")
            for artifact in final.get("artifacts", []):
                card("HTML email saved locally", str(artifact), "good")
            if final.get("order_placed") is False and final.get("email_transmitted") is False:
                card("Action scope", "No order placed; no email transmitted.")

    orders = {p.get("request_id"): p for p in grouped.get("composition.order_state", []) if p.get("request_id")}
    orders.update({p["request_id"]: p for p in _records(final.get("orders")) if "request_id" in p})
    for request_id, order in orders.items():
        receipt = _mapping(order.get("receipt"))
        body = _text(order.get("message"))
        if receipt:
            body += f"\nMock receipt: {receipt.get('order_id', '?')}\n" + _cart(receipt.get("report"))
        card(f"{request_id}: {order.get('status', 'not recorded')}", body, _tone(order.get("status")))

    if grouped.get("delivery.checked"):
        delivery = last("delivery.checked")
        ready = delivery.get("ready") is True
        required = delivery.get("required_menus", [])
        label = "passed" if ready else "incomplete"
        body = "No accepted menus required a tasting plan." if ready and not required else (
            "Required menus: " + ", ".join(map(str, required)) +
            "\nDelivered menus: " + ", ".join(map(str, delivery.get("delivered_menus", []))))
        body += "\n" + "\n".join(map(str, delivery.get("errors", [])))
        card(f"Tasting-plan delivery check: {label}", body.strip(), _tone(label))
    elif final_type == "composition.result" or (final_type == "harness.result" and run.get("case_id") == "tasting-plan"):
        card("Tasting-plan delivery check: not recorded", "Order status alone does not establish delivery.")

    finish = last("run.finished")
    execution = finish.get("status", run.get("status", "not recorded"))
    if execution in {"error", "blocked", "stopped"}:
        card(f"Execution: {execution}", _text(finish.get("error") or run.get("error")), _tone(execution))
    if not final_event:
        card("Final result not recorded", "This view contains only the evidence recorded so far.", "wait")

    for event in events:
        name, p = event.get("event_type"), _mapping(event.get("payload"))
        if name == "model.request":
            step("Agent activity", f"Model call {p.get('call_number', '?')} · {len(p.get('messages', []))} messages supplied · event #{event.get('sequence', '?')}")
        elif name == "model.response":
            calls = [str(c.get("name", "unnamed tool")) for m in _records(p.get("messages"))
                     for c in _records(m.get("contents")) if c.get("type") == "function_call"]
            step("Agent activity", "Model requested: " + ", ".join(calls) if calls else "Model response recorded; final application answer is shown separately.")
        elif name == "tool.requested":
            step("Agent activity", "Tool requested: " + str(p.get("name", p.get("tool", "not recorded"))))
        elif name == "tool.completed":
            step("Agent activity", "Tool returned: " + str(p.get("name", p.get("tool", "not recorded"))) + " · check its result for the business outcome")
        elif name == "workflow.step":
            step("Workflow steps", str(p.get("executor_id", "Unspecified step")).replace("_", " "))
        elif name == "cart.validated":
            violations = _records(p.get("violations"))
            attempt = f" · attempt {p['attempt']}" if "attempt" in p else ""
            step("Cart checks", ("Failed" if violations else "Passed") + attempt + ": " +
                 ("; ".join(str(v.get("detail", v.get("rule", "Unknown check"))) for v in violations)
                  if violations else _money(p.get("subtotal_cents"))))
        elif name == "approval.not_required":
            step("Human review", "Manager review not required for this cart.")
        elif name in {"approval.requested", "email.approval_requested"}:
            label = "Manager review" if name == "approval.requested" else "Email review"
            detail = _money(_mapping(p.get("report")).get("subtotal_cents")) if name == "approval.requested" else str(_mapping(p.get("draft")).get("recipient", "recipient not recorded"))
            scope = ticket_requests.get(p.get("ticket_id"))
            step("Human review", (f"{scope} · " if scope else "") + f"{label} requested: {detail}")
        elif name in {"approval.decided", "email.approval_decided"}:
            label = "Manager" if name == "approval.decided" else "Email reviewer"
            decision = {"approve": "approved", "decline": "declined"}.get(p.get("decision"), "decision not recorded")
            scope = ticket_requests.get(p.get("ticket_id"))
            step("Human review", (f"{scope} · " if scope else "") + f"{label} {decision} · source: {p.get('source', 'not recorded')}")
        elif name == "composition.order_state":
            step("Order workflow results", f"{p.get('request_id', '?')}: {p.get('status', 'not recorded')}")
        elif name == "context.selected":
            step("Context", f"{p.get('mode', 'unspecified')} context · {len(p.get('sources', []))} sources")
            for item in _records(p.get("sources")):
                step("Context", f"{item.get('source', '?')}: {item.get('reason', '')}")
            for item in _records(p.get("excluded_products")):
                step("Context", f"Excluded {item.get('product_id', '?')}: " + ", ".join(map(str, item.get("reasons", []))))
        elif name == "compaction.applied":
            step("Context", f"History shortened: {p.get('before_characters', '?')} → {p.get('after_characters', '?')} characters · input event {p.get('input_event_id', 'not recorded')}")
        elif name in {"skill.loaded", "skill.resource_read"}:
            step("Skills and supporting files", ("Skill: " if name == "skill.loaded" else "Resource: ") + str(p.get("path", "not recorded")))
        elif name == "injection.checked":
            step("Document checks", ("Flagged: " + ", ".join(map(str, p.get("signals", [])))) if p.get("flagged") else "Document not flagged; it remains untrusted data.")
        elif name in {"tool.blocked", "harness.action_blocked", "model.failed", "tool.failed"}:
            step("Blocked actions and errors", f"{name}: {p.get('name', '')} {p.get('reason', p.get('error', 'Details not recorded'))}")
        elif name == "policy.decision" and p.get("outcome") in {"block", "require_approval"}:
            step("Policy checks", f"{p.get('policy_id', '?')} → {p['outcome']}: {p.get('reason', '')}")

    for task in _records(final.get("tasks", last("plan.updated").get("items"))):
        step("Final task list", ("Done: " if task.get("is_complete") else "Open: ") + str(task.get("title", "Untitled task")))
    for preference in final.get("preferences", last("memory.selected").get("preferences", [])):
        step("Saved preferences used", str(preference))
    layers = snapshot.get("layers")
    tracing = (any(layer.get("name") == "trace" for layer in _records(layers)) if isinstance(layers, list) else None)
    return {"requests": _requests(snapshot, events, ask), "answer": answer, "cards": cards,
            "groups": groups, "events": events, "execution": execution,
            "mode": run.get("mode", last("run.started").get("mode", "not recorded")),
            "trace_enabled": tracing, "run": run, "snapshot": snapshot}


def _event_label(event):
    name, p = event.get("event_type", "unknown"), _mapping(event.get("payload"))
    label = name
    if name.startswith("model."):
        label += f" · call {p.get('call_number', '?')}"
    if p.get("name"):
        label += f" · {p['name']}"
    if p.get("request_id"):
        label += f" · {p['request_id']}"
    if name == "tool.completed":
        label += " · returned (see result for business outcome)"
    return f"#{event.get('sequence', '?')} · {label}"


def render_chat(events, *, run=None, snapshot=None, title=None, ask=None, backstage=True):
    """Return self-contained HTML. All recorded text is escaped, including evidence."""
    view = build_chat(events, run=run, snapshot=snapshot, ask=ask)
    run = view["run"]
    title = title or f"Act {run.get('act', '?')} · {run.get('case_id', 'Recorded lesson')}"
    mode = {"fixture": "Scripted responses", "live": "Live model run", "recorded": "Recorded run"}.get(view["mode"], "Run mode not recorded")
    badges = [mode, f"Execution: {view['execution']}", "Recorded view · not a live chat"]
    if view["trace_enabled"] is False:
        badges.append("Detailed model tracing disabled")
    h = [CSS, '<section class="fg-view" aria-label="Recorded agent lesson">',
         f'<header><h2>{escape(str(title))}</h2><div class="fg-badges">',
         *(f'<span class="fg-badge">{escape(b)}</span>' for b in badges), '</div></header>',
         f'<div class="fg-grid {"two" if backstage else ""}"><section class="fg-pane"><h3>💬 Request and response</h3>']
    for request in view["requests"]:
        h.extend([f'<div class="fg-label fg-right">{escape(request["label"])}</div>',
                  f'<div class="fg-bubble fg-user fg-body">{_prose(request["text"])}</div>'])
    if not view["requests"]:
        h.append('<p class="fg-note">Task brief not recorded.</p>')
    if view["answer"]:
        h.extend(['<div class="fg-label">Assistant · final application response</div>',
                  f'<div class="fg-bubble fg-body">{_prose(view["answer"])}</div>'])
    h.append('<h3>Application results</h3>')
    for card in view["cards"]:
        h.append(f'<article class="fg-card {card["tone"]}"><h4>{escape(card["title"])}</h4>'
                 f'<div class="fg-body">{_prose(card["body"])}</div></article>')
    h.append('</section>')
    if backstage:
        h.append('<section class="fg-pane fg-back"><h3>🔎 Steps and checks</h3>')
        h.append('<p class="fg-note">Recorded actions and application state, not the model’s private reasoning.</p>')
        if view["trace_enabled"] is False:
            h.append('<p class="fg-note">Detailed model tracing was disabled. Required audit events and final results can still be shown.</p>')
        focus = {1: {"Agent activity"}, 2: {"Context"}, 3: {"Workflow steps"},
                 4: {"Final task list"}, 5: {"Skills and supporting files"}, 6: {"Order workflow results"}}
        expanded = {"Cart checks", "Human review", "Blocked actions and errors"} | focus.get(run.get("act"), set())
        for label, rows in view["groups"].items():
            opened = ' open' if label in expanded else ''
            h.append(f'<details{opened}><summary>{escape(label)} · {len(rows)}</summary><ul>')
            h.extend(f'<li>{escape(row)}</li>' for row in rows)
            h.append('</ul></details>')
        if not view["groups"]:
            h.append('<p class="fg-note">No summarized steps were recorded. Inspect available evidence below.</p>')
        h.append(f'<details><summary>Inspect recorded events · {len(view["events"])}</summary>'
                 '<p class="fg-note">Internal model replies stay here. Events without actor/request labels remain unscoped; sequence numbers identify each record.</p>')
        for event in view["events"]:
            h.append(f'<details class="fg-evidence"><summary>{escape(_event_label(event))}</summary>'
                     f'<pre>{escape(_preview(event.get("payload")))}</pre></details>')
        h.append('</details><details><summary>Inspect recorded task fields</summary>'
                 f'<pre>{escape(_preview({k: view["snapshot"].get(k) for k in ("request", "context")}))}</pre></details></section>')
    h.append('</div><footer>Classroom simulation · no real purchase or email. Run: '
             + escape(str(run.get("run_id", "not supplied"))) + '</footer></section>')
    return "".join(h)


def render_run(source, run_id, *, title=None, backstage=True):
    """Return HTML from a database path or open Recorder; useful outside notebooks."""
    return render_chat(**load_run(source, run_id), title=title, backstage=backstage)


def _display_html(html):
    try:
        from IPython.display import HTML, display
    except ImportError as exc:
        raise ImportError("show_chat needs IPython in the notebook kernel; render_run returns HTML without it.") from exc
    display(HTML(html))


def show_chat(source, run_id, *, title=None, backstage=True):
    """Display a recorded run without rerunning it; path input survives cell scopes."""
    _display_html(render_run(source, run_id, title=title, backstage=backstage))
