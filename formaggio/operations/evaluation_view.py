"""Read-only evaluation scorecard with every run's checks, output and recorded trace."""
from collections import Counter
from hashlib import sha256
from html import escape
import json
from statistics import mean

from formaggio.operations import chat_view


CSS = """<style>
.fg-evaluation{font:14px/1.5 system-ui,sans-serif;color:#172638;background:#fff;border:1px solid #d6dee7;border-radius:14px;padding:20px;margin:16px 0;color-scheme:light}
.fg-evaluation h2{margin:0 0 8px;font-size:21px}.fg-evaluation h3{font-size:16px;margin:20px 0 8px}
.fg-evaluation .ev-scroll{overflow-x:auto}.fg-evaluation table{border-collapse:collapse;width:100%;font-size:13px}
.fg-evaluation th,.fg-evaluation td{padding:9px 12px;border-bottom:1px solid #d6dee7;text-align:left;vertical-align:top}
.fg-evaluation th{background:#f3f6fa}.fg-evaluation .ev-metrics{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}
.fg-evaluation .ev-metric{padding:12px 18px;border:1px solid #d6dee7;border-radius:9px;min-width:110px}
.fg-evaluation .ev-metric strong{display:block;font-size:22px}.fg-evaluation .ev-note{color:#526174}
.fg-evaluation .ev-warning{padding:10px 14px;background:#fff5d9;border-left:4px solid #b17611}
.fg-evaluation .ev-run{margin:12px 0;border:1px solid #d6dee7;border-radius:9px;padding:12px}
.fg-evaluation .ev-run>summary{cursor:pointer;font-weight:600}.fg-evaluation summary:focus-visible{outline:2px solid #195fc5}
.fg-evaluation a{color:#195fc5}.fg-evaluation pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:360px;overflow:auto}
</style>"""


def evaluation_rows(report):
    """Correctness and repeatability are separate; errors/missing runs stay visible."""
    rows = []
    for case in report["case_summary"]:
        runs = [run for run in report["runs"] if run["case_id"] == case["case_id"]]
        # An execution error is not a successful repeated outcome, even if all fail alike.
        observations = [run["observation"] for run in runs
                        if run["run_status"] in {"completed", "blocked"} and run.get("observation")
                        and not any(check["status"] == "error" for check in run["checks"])]
        statuses = Counter(json.dumps(value["normalized_output"]["status"], sort_keys=True)
                           for value in observations)
        largest = max(statuses.values(), default=0)
        all_observations = [run["observation"] for run in runs if run.get("observation")]

        def metric(key, *, usage=False):
            values = [(value.get("usage") or {}).get(key) if usage else value.get(key)
                      for value in all_observations]
            measured = [value for value in values if type(value) in (int, float)]
            return {"mean": mean(measured) if measured else None, "measured": len(measured)}

        rows.append({"case_id": case["case_id"], "scheduled": case["scheduled"],
                     "passed": case["passed"], "recorded": len(runs),
                     "outcome_agreement": largest / case["scheduled"],
                     "dominant_outcome_runs": largest,
                     "outcomes": [json.loads(value) for value in statuses],
                     "structured_variants": len(case["output_variants"]),
                     "response_variants": case["distinct_response_texts"],
                     "tool_paths": case["distinct_tool_paths"],
                     "latency": metric("duration_seconds"), "model_calls": metric("model_calls"),
                     "input_tokens": metric("input_token_count", usage=True),
                     "output_tokens": metric("output_token_count", usage=True),
                     "untested_checks": sorted(set(case["unexercised_checks"]))})
    return rows


def _text(value):
    return escape(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))


def _table(headers, rows):
    return ('<div class="ev-scroll"><table><thead><tr>'
            + ''.join(f'<th scope="col">{escape(h)}</th>' for h in headers)
            + '</tr></thead><tbody>'
            + ''.join('<tr>' + ''.join(f'<td>{cell}</td>' for cell in row) + '</tr>' for row in rows)
            + '</tbody></table></div>')


def render_evaluation(source, report):
    """Render a saved agent-evaluation report; no model calls, writes or approvals."""
    rows = evaluation_rows(report)
    prefix = "eval-" + sha256(str(report["label"]).encode()).hexdigest()[:12]
    h = [CSS, '<section class="fg-evaluation" aria-label="Evaluation results">',
         '<h2>Evaluation results · ' + _text(report["label"]) + '</h2>']
    if report["mode"] == "fixture":
        h.append('<p class="ev-warning">SCRIPTED RESPONSES · These results test the evaluation machinery. '
                 'They do not measure live model quality or robustness.</p>')
    else:
        h.append('<p class="ev-note">LIVE MODEL · Repeated observations, not a guarantee of future behavior.</p>')
    h.append('<p class="ev-note">Act ' + _text(report["act"]) + ' · Model: '
             + _text(report["model"]) + '</p>')
    scheduled = report["expected_runs"]
    for label, value in (("Passed / scheduled", f'{report["passed"]}/{scheduled}'),
                         ("Runs with failed checks", report["failed"]), ("Run errors", report["errors"]),
                         ("Missing runs", len(report["missing_runs"]))):
        if label == "Passed / scheduled":
            h.append('<div class="ev-metrics">')
        h.append(f'<div class="ev-metric"><strong>{_text(value)}</strong>{escape(label)}</div>')
    h.append('</div><h3>Correctness and repeatability by request</h3>')
    h.append(_table(["Case", "Passed / scheduled", "Outcome agreement", "Observed outcomes",
                     "Distinct structured results", "Distinct response texts", "Distinct tool paths"],
        [[_text(row["case_id"]), f'{row["passed"]}/{row["scheduled"]}',
          f'{row["outcome_agreement"]:.0%} ({row["dominant_outcome_runs"]}/{row["scheduled"]})',
          _text(row["outcomes"]), str(row["structured_variants"]), str(row["response_variants"]),
          str(row["tool_paths"])] for row in rows]))
    h.append('<p class="ev-note">Outcome agreement is the most common completed business status divided by '
             'all scheduled repeats. It can be high even when the checks fail. Different valid menus or wording '
             'are allowed. Structured-result variants include recorded error outcomes. Use at least two repeats; '
             'small samples and exact-text differences do not establish robustness.</p><h3>Effort per run</h3>')

    def metric(row, key):
        value = row[key]
        measured = f'{value["measured"]}/{row["scheduled"]} measured'
        return f'{value["mean"]:.1f} ({measured})' if value["mean"] is not None else f'Unavailable ({measured})'

    h.append(_table(["Case", "Mean model calls", "Mean input tokens", "Mean output tokens", "Mean seconds"],
                   [[_text(row["case_id"]), *(metric(row, key) for key in
                     ("model_calls", "input_tokens", "output_tokens", "latency"))] for row in rows]))
    h.append('<p class="ev-note">Model calls include inner and outer agents. Metrics use available recordings, '
             'including partial/error runs; missing measurements are not zero. Token counts are not dollar costs. '
             'Latency includes local tools and scripted review handling.</p>')
    gaps = [(row["case_id"], check) for row in rows for check in row["untested_checks"]]
    if gaps:
        h.append('<p class="ev-warning">Untested paths: ' + '; '.join(_text(case + ' · ' + check) for case, check in gaps)
                 + '. A passing run may not have reached the manager-approval threshold.</p>')
    h.append('<p class="ev-note">Human prose review remains separate: read for clarity, truthful action status, '
             'and usefulness. A passing structural check is not a prose-quality score.</p><h3>Every recorded run</h3>')
    run_rows = []
    for index, run in enumerate(report["runs"]):
        result = "PASS" if run["passed"] else "ERROR" if (run["run_status"] not in {"completed", "blocked"}
                 or any(check["status"] == "error" for check in run["checks"])) else "FAIL"
        run_rows.append([_text(run["case_id"]), str(run["repetition"]), result,
                         f'<a href="#{prefix}-{index}">Open checks and trace</a>', _text(run["run_id"])])
    h.append(_table(["Case", "Repeat", "Grade", "Evidence", "Run ID"], run_rows))
    for missing in report["missing_runs"]:
        h.append('<p class="ev-warning">MISSING · ' + _text(missing["case_id"])
                 + ' · repeat ' + _text(missing["repetition"]) + ' · no trace exists for this scheduled run.</p>')
    for index, run in enumerate(report["runs"]):
        h.append(f'<details class="ev-run" id="{prefix}-{index}"><summary>' + _text(run["case_id"])
                 + ' · repeat ' + _text(run["repetition"]) + (' · PASS' if run["passed"] else ' · NEEDS REVIEW')
                 + '</summary>')
        h.append(_table(["Check", "Result", "Expected", "Observed", "Explanation"],
            [[_text(check[key]) for key in ("check_id", "status", "expected", "observed", "explanation")]
             for check in run["checks"]]))
        try:
            h.append(chat_view.render_run(source, run["run_id"]))
        except (FileNotFoundError, ValueError) as exc:
            h.append('<p class="ev-warning">Trace unavailable: ' + _text(str(exc)) + '</p>')
        h.append('</details>')
    h.append('<p class="ev-note">This view only reads saved results. Expand each run for its final output and '
             'recorded model/tool events. Large payload previews may be shortened; the notebook JSON export '
             'contains complete recorded events.</p></section>')
    return ''.join(h)


def show_evaluation(source, report):
    """Display the scorecard in Colab/Jupyter; IPython is imported only for display."""
    chat_view._display_html(render_evaluation(source, report))
