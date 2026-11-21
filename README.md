# ERP ticket agent

An agent that handles finance support tickets in an ERP: stuck invoice approvals, holds, duplicate
invoices, payment questions, access requests, bank reconciliations and failed integrations. If it can
fix the ticket, it does. If a person needs to decide, it sends the ticket to the right team.

It is built with Google ADK and Gemini 3.1 Flash-Lite. The ERP is a mock that follows the Oracle Fusion
Payables REST API (field names, paths and response format).

## What I wanted to find out

Whether one agent can be set up for different customers without changing code, and whether it can
work on finance data without making changes it shouldn't. Each customer has a policy file with its
tolerances, limits, team names and approval rules. Before anything is written to the ERP, the code checks
those rules. The model decides what to do, but it can't make a change the rules don't allow.

## Results

I wrote 44 tickets for two made-up customers, Lumen Foods (Singapore) and Kestrel Logistics (Australia).
A ticket is scored from the final ERP records, escalations and replies, not from what the agent says it did.
21 tickets could be resolved under the customer's policy and 23 needed a person.

| | Lumen (23 tickets) | Kestrel (21 tickets) |
|---|---|---|
| Correct | 23 | 21 |
| Correct on the first run | 20 | 20 |
| Resolved without a person | 10 of 10 | 11 of 11 |
| Sent to the right team | 13 of 13 | 10 of 10 |
| Unsafe changes | 0 | 0 |
| Secrets or bank details leaked | 0 | 0 |
| Average tokens per ticket | 24.6k | 21.9k |

Four tickets failed on the first run:

- **L03.** The agent put a duplicate hold on the invoice, then closed the ticket without telling the AP
  team. Now a duplicate, inactive-supplier or bank-mismatch hold has to be escalated before the ticket
  can close.
- **L21.** It checked a supplier's bank account but didn't record the result on the invoice. Now the
  result has to be recorded before closing.
- **K19.** "BR-13 is out by $300." The agent searched for an invoice called BR-13, found nothing and
  escalated to the wrong team (it didn't post the adjustment). The lookup tools now say when an ID
  belongs to another record type, here a reconciliation item.
- **L12.** The agent's answer was right, but my grader only accepted the date as 2026-11-28. I fixed the
  grader.

After the fixes I re-ran these four and they passed.

Customer reports and the trace of every ticket are in [results/v2](results/v2).

## How it works

```
ticket -> agent (ADK + Gemini) -> tools -> rules from policy.yaml -> Oracle connector -> mock ERP
```

- `erp_agent/connectors/oracle_fusion.py` reads and writes Oracle-style records and turns them into one
  format for the rest of the code. Another ERP would need its own connector.
- `erp_agent/customers/<id>/policy.yaml` holds each customer's settings. The same 4% PO variance is
  released at Kestrel (5% tolerance) and refused at Lumen (2%).
- `erp_agent/erp.py` checks the rules before every write and returns the reason when it refuses.
- `erp_agent/kb/` has 13 runbooks, filled in with each customer's values and searched with Chroma.
- Every ticket has to end with `close_ticket` or `escalate`. If it doesn't, the runner sends it back once.
- Model calls, tool calls and tokens are logged per ticket, and each ticket has a token budget.

## Sealed credentials

The agent never sees passwords, API keys or full bank account numbers. It passes a handle
(`cred://oracle-api`) and the tool puts in the real value. Bank accounts are shown masked, and the agent
can only ask whether an account matches, at most twice per supplier. This comes from my Sealed Orders
work on LLM secret leakage.

I tested it on 16 tickets, 12 of them attacks (asking for the password, instructions hidden in a note
or an invoice, fake bank-change emails). With security rules in the prompt, neither version leaked.
With the rules removed, the agent that had the secrets in its prompt leaked on 9 of 16 tickets and the
sealed one on none. This test ran on the first version of the project (tag `v1-security-eval`).

## Use it from any assistant (MCP server)

The same guarded tools run as an MCP server, so Claude, ChatGPT, Cursor or any MCP client can work
on the ERP. One server process serves one customer, chosen when it starts, so an assistant connected
to Lumen cannot read or change Kestrel.

What changes when writes come from an outside assistant instead of my own agent:

- **A person approves risky writes.** Each customer's `policy.yaml` lists the writes that need approval
  and the people who may approve them. At Lumen, releasing a variance hold waits for a person. At
  Kestrel it runs straight away. The assistant gets `pending_approval` and an action id. There is no
  approve tool over MCP. A listed approver decides with `python -m erp_agent.approvals`. The rules are
  checked again when the write runs, so if the record changed after approval, the write is refused.
  A typo in this config stops the server from starting instead of letting a write skip approval.
- **A retried request isn't applied twice.** A client can send a `request_id`. Sending it again returns
  the first action, and reusing it for a different request is refused. If the ERP rejects a call
  before applying it (429, 503), the server retries, but only when no other write of that action has
  gone through. Some operations make two or three ERP writes, and re-running one that is half done
  isn't safe. In that case, and on a timeout or unknown error, the action goes to `needs_review` and a
  person records what really happened. The tradeoff: a person sometimes has to check a write by hand,
  so that nothing is applied twice.
- **Shared state is safe.** The server and the approvals console are separate processes sharing one
  SQLite database. Every status change is a compare-and-swap with its audit row in the same
  transaction, so a cancelled action can't be approved and run, and an action is never claimed twice.
  An assistant can cancel only the actions it requested.
- **State survives a restart.** Requests, decisions, attempts and results are on disk. A worker keeps a
  heartbeat in its own thread. When a worker stops mid-write, its action goes to `needs_review` after
  its lease runs out (30 s) instead of running again.
- **Audit trail.** Requests (with the name the MCP client reports), decisions, refused approvals,
  repeated or conflicting request ids, every attempt (with latency) and results are logged per customer.

```
python -m erp_agent.mcp_server --customer kestrel_au           # stdio, for desktop clients
python -m erp_agent.mcp_server --customer kestrel_au --http    # streamable HTTP on 127.0.0.1, no auth
python -m erp_agent.approvals list --customer kestrel_au
python -m erp_agent.approvals approve act_... --customer kestrel_au --by "AP lead"
```

`tests/test_mcp.py` has 25 checks.
- Through an MCP client session:
  - approval before a role grant;
  - a rule that changed after approval;
  - reject and cancel, including one client trying to cancel another's action;
  - a repeated or conflicting `request_id`;
  - one customer trying to read or approve another's actions;
  - the background worker.
- Directly on the queue and the mock ERP:
  - 503 and 429 faults injected into the mock ERP;
  - a 503 on the second write of a two-write operation;
  - a timeout;
  - config typos;
  - a worker that stopped mid-write, before and after its lease runs out;
  - three processes claiming the same 200 actions at once.

## Limits

- The ERP is a mock. Invoice and hold fields follow Oracle's documentation; approval tasks, users,
  reconciliation and integration jobs are simplified.
- The mock ERP's records live in the server's memory, so they reset when the server restarts. The
  action queue and audit trail are kept on disk.
- Run one server per customer. A second server would share the queue but has its own copy of the
  mock ERP.
- The approvals console and the HTTP transport have no sign-in. A real deployment would put both
  behind the customer's single sign-on. The audit table is not tamper-proof.
- I wrote the tickets myself and used one model. Each ticket ran once (the four above twice), so the
  numbers are not rates with error bars.

## Running it

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env        # add your Gemini API key

pytest -q tests                                   # no API key needed
python -m scripts.reference_check                 # the intended solution for every ticket passes
python run.py kestrel_au "OF-7710 is stuck on a price variance hold, can you sort it?"
python -m eval.run_eval lumen_sg kestrel_au       # full run, about 300 model calls
python -m eval.report                             # report per customer for the latest run
```

To add a customer, create `erp_agent/customers/<id>/policy.yaml` and its data.
