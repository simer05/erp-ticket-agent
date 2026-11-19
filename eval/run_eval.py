"""Run every test ticket through the agent and check the final ERP state.

A ticket passes only if the ERP ends in the expected state AND the right teams were
escalated to. The model's own summary is not trusted: we check the data.

Usage:  python -m eval.run_eval            (all tickets)
        python -m eval.run_eval T03 T10    (selected tickets)
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from erp_agent.runner import run_ticket_sync

HERE = Path(__file__).parent
PAUSE = float(os.environ.get("ERP_EVAL_PAUSE_S", "15"))  # stay under free-tier rate limits


def check(expect, state):
    db, esc = state["db"], state["escalations"]
    problems = []
    for inv_id, fields in expect.get("invoice", {}).items():
        for k, v in fields.items():
            got = db["invoices"][inv_id].get(k)
            if got != v:
                problems.append(f"{inv_id}.{k} = {got!r}, expected {v!r}")
    for user_id, roles in expect.get("user_roles", {}).items():
        got = sorted(db["users"][user_id].get("erp_roles", []))
        if got != sorted(roles):
            problems.append(f"{user_id} roles = {got}, expected {roles}")
    for item_id, fields in expect.get("recon", {}).items():
        for k, v in fields.items():
            got = db["reconciliation"][item_id].get(k)
            if got != v:
                problems.append(f"{item_id}.{k} = {got!r}, expected {v!r}")
    teams = sorted({e["team"] for e in esc})
    if "escalations" in expect and teams != sorted(expect["escalations"]):
        problems.append(f"escalated to {teams}, expected {sorted(expect['escalations'])}")
    if expect.get("escalations_any") and not teams:
        problems.append("expected an escalation, none raised")
    return problems


def main(selected):
    tickets = json.loads((HERE / "tickets.json").read_text(encoding="utf-8"))
    if selected:
        tickets = [t for t in tickets if t["id"] in selected]
    out_dir = HERE / "runs" / datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for i, t in enumerate(tickets):
        if i:
            time.sleep(PAUSE)
        try:
            r = run_ticket_sync(t["ticket"])
            problems = check(t["expect"], r["state"])
        except Exception as e:  # keep going, record the failure
            r, problems = {"tokens": 0, "model_calls": 0, "tool_calls": 0, "trace": [], "final_text": ""}, [f"error: {e}"]
        refused = sum(1 for s in r["trace"] if s.get("type") == "tool" and s.get("refused"))
        row = {"id": t["id"], "category": t["category"], "pass": not problems, "problems": problems,
               "tokens": r["tokens"], "model_calls": r["model_calls"], "tool_calls": r["tool_calls"],
               "guardrail_refusals": refused, "ownership_retry": r.get("ownership_retry", False)}
        rows.append(row)
        (out_dir / f"{t['id']}.json").write_text(json.dumps({**row, "ticket": t["ticket"], **r}, indent=1, default=str), encoding="utf-8")
        print(f"{t['id']} {'PASS' if row['pass'] else 'FAIL'}  tokens={row['tokens']:>6}  tools={row['tool_calls']:>2}  "
              f"refusals={refused}  retry={row['ownership_retry']}  {'; '.join(problems)}")

    n = len(rows)
    passed = sum(r["pass"] for r in rows)
    summary = {
        "model": os.environ.get("ERP_AGENT_MODEL", "gemini-2.5-flash"),
        "tickets": n, "passed": passed, "pass_rate": round(passed / n, 3) if n else 0,
        "avg_tokens": round(sum(r["tokens"] for r in rows) / n) if n else 0,
        "avg_tool_calls": round(sum(r["tool_calls"] for r in rows) / n, 1) if n else 0,
        "rows": rows,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(f"\n{passed}/{n} passed  |  avg tokens {summary['avg_tokens']}  |  avg tool calls {summary['avg_tool_calls']}")
    print(f"details: {out_dir}")


if __name__ == "__main__":
    main(set(sys.argv[1:]))
