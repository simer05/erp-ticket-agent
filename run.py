"""Run one ticket for one customer and print what the agent did.

    python run.py lumen_sg "NW-2210 is on hold, Northwind says it's correct. Please release."
    python run.py kestrel_au "OF-7710 is on a price variance hold, can you release it?"
"""
import json
import sys

from erp_agent.erp import list_customers
from erp_agent.runner import run_ticket_sync

if __name__ == "__main__":
    args = sys.argv[1:]
    customer = args.pop(0) if args and args[0] in list_customers() else "lumen_sg"
    ticket = " ".join(args) or "Acme invoice AC-1001 has been stuck in approval for weeks, please fix."
    r = run_ticket_sync(ticket, customer)
    print(f"\n--- Customer: {customer} ---")
    print("\n--- Agent summary ---\n" + r["final_text"])
    print("\n--- Changes, escalations and closure (audit log) ---")
    for a in r["audit_log"]:
        print(json.dumps(a))
    print("\n--- Steps ---")
    for s in r["trace"]:
        if s["type"] == "tool":
            print(f"tool  {s['name']}({json.dumps(s['args'])}){'  REFUSED: ' + s['result']['refused'] if s['refused'] else ''}")
        else:
            print(f"{s['type']:5} {s.get('tokens', s.get('result', ''))}")
    print(f"\nTotal tokens {r['tokens']}, model calls {r['model_calls']}, tool calls {r['tool_calls']}, "
          f"ERP API calls {r['api_calls']}")
