"""Turn an evaluation run into a one-page report per customer, the kind an engineer sends the
customer's finance lead after a week of tickets.

    python -m eval.report                 (latest run)
    python -m eval.report eval/runs/<dir>
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent

PLAIN = {
    "stuck_approver_left": "Approval stuck: approver left", "stuck_approver_on_leave": "Approval stuck: approver on leave",
    "stuck_approver_on_leave_chain": "Approval stuck: approver on leave", "stuck_approver_active": "Approval slow: reminder",
    "variance_above_tolerance": "PO variance above tolerance", "variance_within_tolerance": "PO variance within tolerance",
    "revalidate_within_tolerance": "Needs revalidation", "revalidate_above_tolerance": "Needs revalidation, over PO",
    "duplicate": "Possible duplicate", "inactive_supplier": "Inactive supplier",
    "rejected_no_resubmit_policy": "Rejected invoice", "rejected_resubmit_allowed": "Rejected invoice: approver away",
    "rejected_data_error": "Rejected invoice: data error", "workflow_error_one_requester": "Approval workflow error",
    "status_paid": "Payment status question", "status_payment_date": "Payment date question",
    "injection_in_invoice_field": "Status question (suspicious invoice text)", "access_ok": "Role request",
    "access_pending": "Role request without approval", "access_sod_conflict": "Role request: duties conflict",
    "recon_small_fx": "Reconciliation: FX rounding", "recon_fx_within_limit": "Reconciliation: FX rounding",
    "recon_bank_fee": "Reconciliation: bank fee", "recon_large": "Reconciliation: large difference",
    "recon_fx_above_limit": "Reconciliation: FX above limit", "integration_failure": "Failed integration",
    "bank_check_before_payment": "Bank check before payment", "bank_change_request": "Supplier bank change request",
    "pressure": "Urgent request to skip checks",
}


def pct(a, b):
    return f"{round(100 * a / b)}%" if b else "n/a"


def report(run_dir, cid, s, model):
    rows = s["rows"]
    lines = [f"# Weekly agent report: {s['customer']}", "",
             f"Mock data, {s['tickets']} test tickets, model {model}. Every result below is checked against the ERP "
             "records, not taken from the agent's own summary.", "", "## Headline", "",
             "| | |", "|---|---|",
             f"| Tickets handled correctly | **{s['passed']} of {s['tickets']}** ({pct(s['passed'], s['tickets'])}) |",
             f"| Resolved with no person involved | **{s['resolved_without_human']} of {s['tickets']}** "
             f"({pct(s['resolved_without_human'], s['tickets'])}; {s['could_be_resolved_without_human']} were resolvable under your policy) |",
             f"| Sent to the right team | {s['routed_correctly']} of {s['needed_a_human']} |",
             f"| Unsafe changes to your data | {s['tickets_with_unsafe_change']} |",
             f"| Credentials or bank details exposed | {s['tickets_with_leak']} |",
             f"| Average cost per ticket | {s['avg_tokens']:,} tokens, {s['avg_tool_calls']} tool calls |", ""]

    lines += ["## What came in and what happened", "", "| Ticket type | Tickets | Handled correctly | Ending |",
              "|---|---|---|---|"]
    groups = defaultdict(list)
    for r in rows:
        groups[PLAIN.get(r["category"], r["category"])].append(r)
    for name, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        ending = "sent to a team" if all(r["needs_human"] for r in rs) else "closed by the agent"
        lines.append(f"| {name} | {len(rs)} | {sum(r['pass'] for r in rs)} | {ending} |")

    refusals = sum(r["refusals"] for r in rows)
    lines += ["", "## Guardrails", "",
              f"The rules layer refused {refusals} attempted actions that broke your policy (for example releasing a hold "
              "above tolerance or reassigning to an approver whose limit is too low). In each case nothing changed and "
              f"the agent followed the procedure instead. The ownership check sent {s['ownership_retries']} ticket(s) back "
              "because they were neither closed nor escalated."]

    fails = [r for r in rows if not r["pass"]]
    lines += ["", "## Needs your review", ""]
    if not fails:
        lines.append("Nothing this week.")
    for r in fails:
        lines.append(f"- {r['id']} ({PLAIN.get(r['category'], r['category'])}): {'; '.join(r['problems'] + [str(l) for l in r['leaks']])}")
    lines += ["", "## Suggested next steps", "",
              "- Tickets that always end with a team (for example data-error rejections) are candidates for a policy change "
              "if your team wants the agent to handle them.",
              "- Failed integrations and approval-routing errors point to HR and integration data; fixing those at the source "
              "removes the tickets entirely."]
    out = run_dir / f"report_{cid}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main(arg):
    run_dir = Path(arg) if arg else max((p for p in (HERE / "runs").iterdir() if (p / "summary.json").exists()),
                                        key=lambda p: p.name)
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    for cid, s in summary["customers"].items():
        print(report(run_dir, cid, s, summary["model"]))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
