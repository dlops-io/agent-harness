# Building an Agent Harness

This hands-on tutorial brings together the concepts from the lecture: **context engineering, agent harnesses, skills, agent systems, and evaluation**. You will explore a cheese-shop assistant, add controlled workflows and human review, and inspect the evidence of what the system actually did.

The application is already implemented. Your job is to run it, read the relevant code, compare behaviors, and explain which parts are controlled by the model and which are enforced by the application.

## What you'll build and explore

The goal: **help a customer plan a cheese tasting while preserving their constraints and controlling actions**.

The six executable acts follow the lecture concepts:

| Lecture concept | Tutorial activity | Key question |
|---|---|---|
| **1. Recap** | Act 1: an agent with instructions and tools | What can a tool-using agent propose? |
| **2. Context Engineering** | Act 2: compare basic and enriched context | What information should the model receive? |
| **3. Agent Harness** | Act 3: a controlled workflow; Act 4: planning, memory, compaction, and review | How does the runtime manage execution and enforce boundaries? |
| **4. Skills** | Act 5: load reusable instructions and resources when needed | How do skills guide work without granting permissions? |
| **5. Agent Systems** | Act 6: a harness invokes an ordering workflow | How do flexible planning and controlled execution work together? |
| **6. Evaluation** | Inspect traces, run repeated cases, and compare configurations | What evidence supports a claim that the system improved? |
| **7. Additional Resources** | Read the code, prompts, policies, and skill resources | Where is each behavior implemented? |

**Observability and governance run through every act.** Watch both the agent's response and the application's recorded decisions.

The acts are separate demonstrations. Follow them in order to learn, but do not expect one command's inventory or order to carry into the next command.

> All catalog data and shop policies are classroom examples. Orders and inventory changes are mock, in-memory operations. Vendor outreach produces a reviewed local HTML file; it never transmits email, purchases stock, or reserves stock with a supplier.

## Contents

- [Setup and verification](#setup-and-verification)
- [1. Recap — agent and tools](#1-recap--agent-and-tools)
- [2. Context Engineering — choose what the model sees](#2-context-engineering--choose-what-the-model-sees)
- [3. Agent Harness — manage execution and enforce boundaries](#3-agent-harness--manage-execution-and-enforce-boundaries)
- [4. Skills — load reusable guidance when needed](#4-skills--load-reusable-guidance-when-needed)
- [5. Agent Systems — compose a harness and a workflow](#5-agent-systems--compose-a-harness-and-a-workflow)
- [6. Evaluation — inspect, repeat, and compare](#6-evaluation--inspect-repeat-and-compare)
- [7. Additional Resources](#7-additional-resources)
- [Troubleshooting](#troubleshooting)

---

## Setup and verification

### Prerequisites

- Docker installed and running.
- A local copy of this repository, with a terminal open in the `agent-harness` directory.
- An OpenAI API key and access to the selected model **for live runs**. Offline tests and scripted fixtures do not need a model credential.

The Docker image installs Python 3.13 and the dependencies declared in `pyproject.toml` and `uv.lock`.

### Start the container

Run these commands **on your host machine**, from `agent-harness`:

```bash
# The launcher retains this mount for future GCP/Gemini support.
mkdir -p ../secrets

# Live runs only: replace the placeholder with your own credential.
export OPENAI_API_KEY="YOUR_OPENAI_API_KEY"

bash docker-shell.sh
```

Skip the `export` line if you are following the offline path. Keep credentials out of source files and commits.

The script builds and starts the container, or opens a shell in the existing `agent-harness` container. The repository is mounted at `/app`, and the entrypoint activates the Python environment.

**Run every remaining command inside the container, from `/app`, unless a step explicitly says otherwise.**

If the container was already running when you changed the host's API key, the new value is not automatically forwarded by `docker exec`. Set it in the container shell before running live commands. In its Bash shell, you can enter it without displaying it or putting its value in the command history:

```bash
read -rsp "OpenAI API key: " OPENAI_API_KEY
export OPENAI_API_KEY
printf '\n'
```

### Verify the environment

These commands are **offline**:

```bash
python -m scripts.check_sdk
python -m tests
```

The SDK check prints installed versions and available capabilities. The test suite should finish with `OK`. It exercises local scripted model responses, validation, approvals, isolation, and failure handling; it does not establish live model quality.

Passing-test output is buffered by default. To inspect all diagnostic output, including deliberate failure tests:

```bash
python -m tests --show-logs
```

### Choose the live model

The current implementation uses an OpenAI client. Model selection is `--model`, then `OPENAI_CHAT_MODEL`, then the default in [formaggio/config.py](formaggio/config.py).

To inspect the current selection without making a model call:

```bash
python -c 'from formaggio.config import MODEL; print(MODEL)'
```

If your instructor specifies another supported OpenAI model, set `OPENAI_CHAT_MODEL` **inside the container** before running the walkthrough, or add `--model YOUR_OPENAI_MODEL_ID` to a live command. Replace that placeholder with a model ID available to your account.

### GCP and Gemini — TODO

The GCP settings in [docker-shell.sh](docker-shell.sh) are intentionally retained for a future Gemini option:

- `GCP_PROJECT` identifies the project.
- The host's `../secrets/` directory is mounted at `/secrets`.
- `GOOGLE_APPLICATION_CREDENTIALS` currently points to `/secrets/ml-workflow.json`.

**No GCP service-account setup is required for the current walkthrough.** Gemini switching is not implemented; changing `--model` to a Gemini name does not switch providers.

**TODO:** add explicit OpenAI/Gemini provider selection, the Gemini client and authentication setup, and checks that structured outputs, tools, approvals, tracing, and evaluation work with both providers. The intended student experience is to change provider/model configuration while keeping the tutorial activities the same.

### Live runs versus fixtures

Commands marked **live** call the configured model and may incur API charges. Their wording, tool choices, carts, and completion behavior can vary.

Commands with `--fixture` use fixed proposals or local scripted responses. They demonstrate execution paths without a remote model call. Standalone `--fixture` is supported for Acts 3–6; Acts 1–2 have offline context preview and fixture evaluation batches instead.

Flags such as `--manager-decision approve` and `--email-decision decline` supply explicit **test decisions**. Without them, the host asks you interactively when review is required. The model never supplies the manager or email approval.

---

## 1. Recap — agent and tools

**Concept: instructions + model + tools.** Start with the familiar agent loop. The model can read catalog facts and propose a cart, but these tools cannot place an order.

### Run Act 1

**Live:**

```bash
python cli.py --act 1 --scenario standard
```

The standard scenario requests a tasting for 12 guests in PA, with a $150 cheese budget, a confirmed nut allergy, at least one French cheese, and at least one cheese with funk level 4 or higher.

This will:

- Supply the confirmed request and shop instructions to the agent.
- Expose catalog, stock, pricing, pairing, and proposal tools.
- Display the agent's response and an independent check of its final cart.
- Record the run without placing an order or silently repairing the proposal.

The console starts with the customer ask in plain language, followed by numbered model/tool calls and compact argument/result summaries. Model steps show the latest tool results in context, elapsed time, and token usage when available.

The ask is rendered from the confirmed fields in [scenarios.json](data/scenarios.json); the fixtures do not store a separate original customer sentence. This display does not change the message sent to the model. To see that exact message, including its structured request, and detailed JSON tool payload previews, add `--show-json`:

```bash
python cli.py --act 1 --scenario standard --show-json
```

`--show-json` also applies to progress logs in the other individual act runs. Long JSON payloads are labeled as truncated previews; use `--inspect-run RUN_ID` for the full recorded model/tool payloads. SQLite tracing remains complete in either display mode. Explicit inspection commands such as `--preview-context` and `--inspect-run`, and the existing `--show-context` option, still display their requested details. These logs describe visible execution, not hidden model reasoning.

**What to look for:** Does the proposal satisfy the independent cart check? Does the response distinguish a proposal from an order? An invalid proposal is useful evidence for the later workflow lesson.

### Try a variation

**Live:**

```bash
python cli.py --act 1 --scenario missing-details
```

The destination and allergy information are missing. Look for clarification instead of invented details.

**Code to read:** [act1_agent.py](acts/act1_agent.py), [tools.py](formaggio/agents/tools.py), and [shop_assistant.md](prompts/shop_assistant.md).

**Checkpoint:** Explain why calling `preview_order` does not authorize or place an order.

---

## 2. Context Engineering — choose what the model sees

**Concept: context is assembled, selected, and attributed.** Keep the model, instructions, and tools fixed while changing the information supplied before the agent runs.

### Inspect the context first

**Offline:**

```bash
python cli.py --preview-context --scenario standard
```

Compare the `basic` and `enriched` packets. Enriched context contains the confirmed request, eligible catalog candidates, and the current customer's saved preferences. It also records excluded products and the reasons for exclusion.

### Run Act 2

**Live; runs both modes in separate sessions:**

```bash
python cli.py --act 2 --scenario standard
```

This will:

- Run the same assistant once with basic context and once with enriched context.
- Keep the prompt, model, tools, and customer brief the same.
- Show both responses, cart checks, and model-call counts.

**What to look for:** Which facts are already available in enriched mode? Which products were filtered out? Did the model need different tool calls? One pair of runs does not establish that enriched context always performs better.

**Code to read:** [act2_context.py](acts/act2_context.py) and [context.py](formaggio/agents/context.py).

**Checkpoint:** Explain why selecting eligible candidates is useful but does not replace validation of the final cart. Current confirmed constraints remain authoritative over saved preferences.

---

## 3. Agent Harness — manage execution and enforce boundaries

**Concept: the runtime supplies state, control, limits, and permissions around model calls.** Explore two execution patterns: a fixed ordering workflow and a planning harness with tools and human review.

### Act 3: execute a controlled ordering workflow

The application controls this sequence:

```text
Confirm request → Propose cart → Validate and price
                      ↑                 |
                      └── Revise ───────┘  if invalid and revisions remain
                                        |
                                   if valid
                                        ↓
                         Select pairings → Manager review if required
                                        → Revalidate and place mock order
```

Missing details produce clarification; complaints produce escalation. Invalid carts get at most two revisions after the initial proposal. Manager approval is required for a cheese subtotal **greater than $200**, and cannot override another failed rule.

**Live:**

```bash
python cli.py --act 3 --scenario standard
```

**What to look for:** The model proposes quantities, but application code chooses transitions, calculates prices, validates constraints, and places the mock order. A successful order has an application receipt. If a valid cart cannot be obtained, the workflow reports that outcome.

Now make the approval boundary predictable with a **scripted fixture**:

```bash
python cli.py --act 3 --fixture --scenario manager-approval
```

The fixture cart totals $220.80. At the manager prompt, review the request and cart, then type `approve` or `decline`. An approved cart is checked again before placement; declining places no order.

To observe a bounded repair, run this **offline variation**:

```bash
python cli.py --act 3 --fixture --scenario out-of-stock
```

Look for an initial stock violation followed by a revised valid cart. Pairings should correspond to the accepted cart.

**Code to read:** [act3_workflow.py](acts/act3_workflow.py), [store.py](formaggio/shop/store.py), and [checkout.py](formaggio/shop/checkout.py).

**Checkpoint:** Identify what an approval is bound to and why checkout must revalidate after a human pause.

### Act 4: plan with tasks, memory, compaction, and review

**Concept: an agent harness manages more than the model's next tool call.** This act adds a task list, customer-scoped preference memory, bounded execution, context compaction, vendor-document checks, and approval before saving an artifact.

**Live:**

```bash
python cli.py --act 4
```

The event brief specifies 1,000 g Comté, 1,000 g Mimolette, and 1,200 g Époisses for 40 guests in NY. The cheese quote is $177.80. Shop Époisses stock is 900 g, leaving a 300 g shortfall.

This will give the agent tools to:

- Assess the confirmed menu and identify the stock shortfall.
- Maintain a visible task list and retrieve vendor evidence.
- Draft an availability inquiry for the approved recipient.
- Request human review before writing a local mock-email HTML file.

When prompted, type `approve` or `decline`. An approved save prints the file path. The repository is mounted from your host, so the file is also available under the host repository's `outputs/` directory.

**What to look for:** A completed task is not an approval. Shop stock does not establish vendor availability. The authoritative artifact status must agree with the actual save or decline.

To see compaction and untrusted content handling predictably, run this **offline variation**:

```bash
python cli.py --act 4 --fixture --demo-compaction --vendor-document malicious --email-decision decline
```

This seeds explicitly synthetic old history, compacts it, quarantines the fixture's malicious vendor text, and supplies a test decline. Confirmed constraints and review state are restored from application data. No HTML email should be saved.

**Optional memory exercise — live:**

```bash
python cli.py --act 4 --customer shivas --remember-preference "Prefer nonalcoholic pairings today"
python cli.py --act 4 --customer shivas
```

Compare the printed memory on the second visit. The host persists the explicitly supplied preference; the model has no memory-write tool. Default live preference memory survives across runs, while standalone fixtures use isolated memory unless you supply `--memory-db`.

**Code to read:** [act4_harness.py](acts/act4_harness.py), [harness_state.py](formaggio/agents/harness_state.py), [governance.py](formaggio/operations/governance.py), and [vendor_outreach.py](formaggio/shop/vendor_outreach.py).

**Checkpoint:** Distinguish task state, conversation history, preference memory, approval state, and audit records. Which of these can authorize an action?

---

## 4. Skills — load reusable guidance when needed

**Concept: progressive disclosure of reusable instructions and resources.** Act 5 uses the same harness as Act 4, adding native skill discovery and loading.

The available skills are:

- **tasting-planning:** serving guidance and a tasting-plan outline.
- **vendor-outreach:** sourcing guidance and an HTML email template.

The model initially sees skill metadata. It can then load a skill body and read its reference or asset files through tools. Skills guide procedure and presentation; they cannot grant approval or change tool permissions.

### Run Act 5

**Live:**

```bash
python cli.py --act 5
```

**What to look for:** Compare this with Act 4. Which skills and resources were loaded? The vendor email template must be loaded before drafting in this act, and saving still requires human approval. The final output lists loaded skills and resource paths.

### Try selective loading

**Offline:**

```bash
python cli.py --act 5 --fixture --scenario stock-question
```

This answers the Époisses stock question through `get_stock`: **900 g**, with no skill bodies loaded. A simple lookup does not need the full planning procedure.

For a repeatable complete sourcing path, use:

```bash
python cli.py --act 5 --fixture --email-decision approve
```

These scripted responses demonstrate the integration. To observe the model's own skill choices, repeat a command without `--fixture`; omit the test-decision flag for interactive review.

**Code to read:** [act5_skills.py](acts/act5_skills.py), [skill_support.py](formaggio/agents/skill_support.py), [tasting-planning/SKILL.md](skills/tasting-planning/SKILL.md), and [vendor-outreach/SKILL.md](skills/vendor-outreach/SKILL.md).

**Checkpoint:** Explain how a skill differs from a tool, and why reading instructions cannot approve a protected action.

---

## 5. Agent Systems — compose a harness and a workflow

**Concept: compose components with different responsibilities.** The outer assistant manages the customer task. It calls the existing Act 3 workflow as a tool, receives an authoritative outcome, and uses a skill to present the accepted menu.

```text
Customer assistant / harness
  → start_order(request_id)
      → Act 3 ordering workflow
          → cart proposer → validation → host review if needed → checkout
      ← pending review or final outcome
  → tasting-planning skill for an accepted menu
  → customer-facing explanation
```

### Run Act 6

**Live:**

```bash
python cli.py --act 6 --scenario standard
```

**What to look for:** Separate outer-model calls from workflow-model calls. The outer agent cannot replace the confirmed request or approve its own order. After an order is accepted, the tasting plan should use receipt quantities and prices rather than inventing a new cart.

### Try two competing orders

**Offline:**

```bash
python cli.py --act 6 --fixture --scenario two-orders --manager-decision approve
```

This starts two confirmed requests sharing one checkout and inventory. Both request review. The test host approves both, but the first order consumes stock: the second should be blocked by final stock validation.

This will demonstrate:

- Separate workflow handles and approval tickets for each request.
- Host-controlled resume of a pending workflow.
- Shared resource constraints across otherwise valid requests.
- A distinction between approval and successful placement.

**Code to read:** [act6_composition.py](acts/act6_composition.py) and [composition.py](formaggio/agents/composition.py).

**Checkpoint:** Explain why invoking a governed workflow as a tool is useful, and why two approvals do not guarantee two successful orders.

---

## 6. Evaluation — inspect, repeat, and compare

**Concept: collect evidence about outcomes and execution paths.** A convincing answer, a passing unit test, and a successful live run answer different questions.

### Step 1: inspect a recorded run

**Offline; no model calls:**

```bash
python cli.py --list-runs
```

Choose a run ID from the output and replace `RUN_ID` below:

```bash
python cli.py --inspect-run RUN_ID
```

Follow the chronological events: context selection, model and tool calls, validation, policy decisions, approvals, and final outcomes. The recorded model messages are visible inputs and outputs, not hidden model reasoning.

**Checkpoint:** Find the evidence for one claim in the final answer, such as “order placed” or “HTML saved.” Do not infer permission from the agent's prose or task list.

### Step 2: verify the evaluation machinery offline

```bash
python cli.py --act 2 --evaluate core --fixture --case standard --repeats 2 --label context-fixture-1
```

Act 2 evaluates both basic and enriched context, so this schedules **four runs**. Each run has fresh operational state. These scripted outputs check the evaluation machinery; they do not measure the model's context-engineering performance.

Read the saved report without rerunning anything:

```bash
python cli.py --report context-fixture-1
```

Labels are unique and cannot be overwritten. When repeating a batch, choose a new label. Keep the model and other settings fixed when comparing configurations.

### Step 3: run a small live comparison

**Live; four scheduled runs per batch:**

```bash
python cli.py --act 2 --evaluate core --context enriched --case standard --case missing-details --repeats 2 --label context-live-a
```

Create an alternate prompt:

```bash
cp prompts/shop_assistant.md prompts/shop_assistant_v2.md
```

Open the copy in your editor. Make one deliberate change, such as requesting a shorter response with explicit price and action-status labels. Preserve the confirmed-constraint instructions. Do not change source, data, or skills during a batch.

Run the same cases and settings with the changed prompt:

```bash
python cli.py --act 2 --evaluate core --context enriched --case standard --case missing-details --repeats 2 --label context-live-b --prompt-file prompts/shop_assistant_v2.md
```

**Offline comparison:**

```bash
python cli.py --compare context-live-a context-live-b --json-output outputs/context-comparison.json
```

Inspect changed configuration, checks, structured outcomes, response text changes, model-call counts, latency, and token usage when available. Repetition numbers are not matched random seeds, and a small sample does not establish reliability.

### Step 4: judge what the checks do and do not establish

Automated evaluation checks application properties such as cart validity, action counts, approval ordering, and required skill/resource usage. It also records errors and missing runs. A manager case that never reaches the approval threshold can leave that review path unexercised; inspect coverage as well as pass rates.

Final prose needs separate review. Ask whether the response:

- Preserves confirmed allergies and accurately states the customer's requirements.
- Distinguishes shop inventory from unconfirmed vendor availability.
- Uses the accepted menu's quantities and price.
- Reports proposed, pending, declined, blocked, placed, and saved states accurately.
- Supplies the requested tasting plan or explanation.

Evaluation is a collection of specific checks, not a proof of correctness. Scripted fixture success does not establish live response quality, and loading a skill does not prove the agent followed it well.

**Code to read:** [live_evaluation.py](formaggio/evaluation/live_evaluation.py), [evaluation_checks.py](formaggio/evaluation/evaluation_checks.py), [evaluation_reports.py](formaggio/evaluation/evaluation_reports.py), and [agent_evaluation_cases.json](data/agent_evaluation_cases.json).

**Checkpoint:** Report one observed difference between the two configurations, the evidence for it, and a limitation of your experiment.

---

## 7. Additional Resources

### Repository map

`cli.py` is the only top-level Python file. The six lesson modules live in the `acts/` package; shared implementation code lives in `formaggio/`. Run every lesson through the same `python cli.py --act N` command.

| Resource | What to inspect |
|---|---|
| [Act entry points](acts/) (`act1_agent.py` through `act6_composition.py`) | The progression from basic agent to composed system |
| [Agent support](formaggio/agents/) | Context, runtime hooks, memory, compaction, skill access, and workflow handles |
| [Shop logic](formaggio/shop/) | Typed contracts, validation, checkout, and vendor artifacts |
| [Operations](formaggio/operations/) | Governance and SQLite/OpenTelemetry recording |
| [Prompts](prompts/) | Instructions supplied to the models |
| [Skills](skills/) | Skill metadata, instructions, references, and templates |
| [Policies](data/policies.json), [scenarios](data/scenarios.json), and [customers](data/customers.json) | Classroom rules and confirmed inputs |
| [Tests](tests/) | Counterexamples for invalid carts, forged approvals, customer isolation, and audit failures |
| [CLI](cli.py) | Available commands and argument constraints; use `python cli.py --help` |

### What persists between commands?

| State or output | Lifetime / location |
|---|---|
| Runs, events, spans, configuration snapshots, and evaluations | Persist in `outputs/observability.sqlite`; override with `--db` |
| Live Act 4/5 customer preferences | Persist in `outputs/customer_memory.sqlite`; override with `--memory-db` |
| Standalone harness fixture memory | Isolated under `outputs/RUN_ID/memory.sqlite` by default |
| Approved mock HTML email | File under `outputs/RUN_ID/`; evaluation artifacts use `outputs/evaluations/EXPERIMENT_ID/RUN_ID/` |
| Inventory changes, mock orders, approval tickets, and pending workflows | In memory for the current process; no restart recovery |
| Harness task list and conversation history | Current session; not restored across separate CLI commands |

The SQLite trace is an observation of execution, not the operational order database. Starting a new CLI command creates fresh shop inventory. Inspecting an old receipt does not recreate the order.

### End-of-lecture reflection

Choose one completed run and explain how the lecture concepts work together:

1. **Context Engineering:** What facts entered the model context, and where did they come from?
2. **Agent Harness:** What state, limits, and action boundaries did the runtime manage?
3. **Skills:** Which reusable guidance was loaded, and why was it relevant?
4. **Agent Systems:** Which component proposed, validated, approved, and executed the action?
5. **Evaluation:** What evidence supports the result, and what still requires human judgment?

## Troubleshooting

| Symptom | What to check |
|---|---|
| Missing API key or authentication failure | Live commands need a valid key in the container shell. Tests and fixtures do not. The app does not automatically load a `.env` file. |
| Model unavailable | Select an OpenAI model your account can use with `--model`, or set `OPENAI_CHAT_MODEL` inside the container. Gemini support remains TODO. |
| Manager/email input unavailable | Run interactively, or supply an explicit test decision for the relevant act. Evaluation batches supply their own scenario test decisions. |
| “Experiment label already exists” | Use a new batch label; use `--report` to inspect the earlier batch. |
| “Choose a synthetic customer” or a dataset-reference test fails | Customer IDs in scenarios and commands must match `data/customers.json`. Current IDs are `pavlos` and `shivas`. |
| Long error-looking output with `--show-logs` | Negative tests deliberately trigger policy and audit failures. Check the final `OK`/`FAILED` result. The installed SDK can also emit a misleading approval-identity warning on successful resumes; verify the authoritative outcome. |
| A live run reaches a time or call limit | Inspect its trace and partial outcomes. A limit is an intentional bound on execution; a stopped run is not evidence that the requested work completed. Use a fixture to inspect the expected path. |
| Changes to host environment variables do not appear | Reopening an existing container does not refresh its environment. Set the variable in the current container shell. |
