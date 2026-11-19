"""Run one ticket and print what the agent did.

    python run.py "Invoice INV-1001 has been stuck in approval for weeks, please fix"
"""
import json
import sys

from erp_agent.runner import run_ticket_sync

if __name__ == "__main__":
    ticket = " ".join(sys.argv[1:]) or "Invoice INV-1001 has been stuck in approval for weeks, please fix."
    r = run_ticket_sync(ticket)
    print("\n--- Agent summary ---\n" + r["final_text"])
    print("\n--- ERP changes (audit log) ---")
    for a in r["audit_log"]:
        print(json.dumps(a))
    print("\n--- Steps ---")
    for s in r["trace"]:
        if s["type"] == "tool":
            print(f"tool  {s['name']}({json.dumps(s['args'])}){'  REFUSED' if s['refused'] else ''}")
        else:
            print(f"{s['type']:5} tokens={s.get('tokens')}")
    print(f"\nTotal tokens {r['tokens']}, model calls {r['model_calls']}, tool calls {r['tool_calls']}")
