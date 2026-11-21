"""Approval console for writes requested through the MCP server.

  python -m erp_agent.approvals list    --customer lumen_sg          (waiting for approval or review)
  python -m erp_agent.approvals show    --customer lumen_sg act_1234
  python -m erp_agent.approvals approve --customer lumen_sg act_1234 --by "AP lead" --note "checked the PO"
  python -m erp_agent.approvals reject  --customer lumen_sg act_1234 --by "AP lead" --note "wrong approver"
  python -m erp_agent.approvals resolve --customer lumen_sg act_1234 --by "AP lead" --outcome done \
      --note "role is on the user in the ERP"

--by must be one of the approvers in the customer's policy (mcp.approvers). This console is not
authenticated; in a real deployment it would sit behind the customer's sign-in.

Approving only records the decision. The running server's worker picks the action up and executes it,
checking the customer's rules again at that moment. `resolve` closes an action whose outcome was
unknown (needs_review) after a person has checked the ERP.
"""
import argparse
import json

from .actions import NEEDS_REVIEW, PENDING, ActionQueue
from .erp import list_customers, load_customer
from .mcp_server import mcp_policy


def main(argv=None):
    ap = argparse.ArgumentParser(description="Approve, reject or resolve ERP writes requested by an assistant.")
    ap.add_argument("command", choices=["list", "show", "approve", "reject", "resolve"])
    ap.add_argument("action_id", nargs="?")
    ap.add_argument("--customer", required=True, choices=list_customers())
    ap.add_argument("--by", help="your name as listed in the customer's mcp.approvers")
    ap.add_argument("--note", default="")
    ap.add_argument("--outcome", choices=["done", "failed"], help="for resolve: was the write applied?")
    ap.add_argument("--all", action="store_true", help="list every action")
    a = ap.parse_args(argv)
    q = ActionQueue()
    _, approvers = mcp_policy(load_customer(a.customer)[0])

    if a.command == "list":
        rows = q.list(a.customer, None if a.all else (PENDING, NEEDS_REVIEW))
        for r in rows:
            print(f"{r['id']}  {r['status']:<16} {r['tool']:<22} {json.dumps(r['args'])}  by {r['requested_by']}")
        print(f"{len(rows)} action(s)")
        return 0
    if not a.action_id:
        ap.error("action_id is required")
    if a.command == "show":
        print(json.dumps({"action": q.get(a.customer, a.action_id),
                          "audit": q.audit_trail(a.customer, a.action_id)}, indent=2, default=str))
        return 0
    if not a.by:
        ap.error("--by is required")
    if a.command == "approve":
        r = q.approve(a.customer, a.action_id, a.by, approvers, a.note)
    elif a.command == "reject":
        r = q.reject(a.customer, a.action_id, a.by, approvers, a.note)
    else:
        if not a.outcome:
            ap.error("--outcome is required for resolve")
        r = q.resolve(a.customer, a.action_id, a.by, approvers, a.outcome, a.note)
    print(json.dumps(r, indent=2, default=str))
    return 1 if "error" in r else 0


if __name__ == "__main__":
    raise SystemExit(main())
