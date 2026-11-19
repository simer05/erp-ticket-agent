# ERP ticket agent

An AI agent that resolves finance support tickets inside a customer's ERP: stuck invoice approvals,
holds, duplicates, payment questions, access requests, bank reconciliations and failed integrations.
Built with **Google ADK** and **Gemini**, against a mock modelled on the **Oracle Fusion Payables REST API**.

It is built the way a forward deployed engineer would ship it to customers:
1. **Connect to the customer's ERP** through a connector, not hard-coded data.
2. **Onboard a new customer by configuration**: each customer's thresholds, hold names, teams and
   rules live in one policy file. No code changes.
3. **Enforce the customer's rules in code**, so the agent cannot make an unsafe change even if the model
   gets it wrong.
4. **Measure it like a deployment**: share of tickets resolved without a person, routing accuracy,
   unsafe changes, cost per ticket, and a one-page report for the customer.

In addition, it **seals credentials**: the model never sees passwords, keys or full bank accounts
(see [Security](#in-addition-sealed-credentials)).

## Results

44 test tickets across 2 customers, Gemini 3.1 Flash-Lite (free tier). Every result is checked against
the final ERP records, escalations and replies, not against the agent's own summary.

| | Lumen Foods (Singapore) | Kestrel Logistics (Australia) | Total |
|---|---|---|---|
| Tickets handled correctly | 23/23 | 21/21 | **44/44** |
| Correct on the first run | 20/23 | 20/21 | 40/44 |
| Resolved with no person involved (of those the policy allows) | 10/10 | 11/11 | **21/21** |
| Sent to the right team (of those needing a person) | 13/13 | 10/10 | **23/23** |
| Unsafe changes to ERP data | 0 | 0 | **0** |
| Credentials or bank details exposed | 0 | 0 | **0** |
| Average cost per ticket | 24.6k tokens, 6.2 tool calls | 21.9k tokens, 5.5 tool calls | |

The 4 first-run misses, and what changed:

| Ticket | What went wrong | Fix |
|---|---|---|
| L03 duplicate invoice | Placed the duplicate hold, then closed the ticket without telling the AP team | **Closure obligations in code**: a duplicate, inactive-supplier or bank-mismatch hold must be escalated to the owning team before the ticket can close |
| L21 bank check before payment | Checked the account but never recorded the result on the invoice | Same mechanism: a bank verification must be recorded before closing; the tool result says what is required next |
| K19 "BR-13 is out by $300" | Read BR-13 as an invoice, found nothing, escalated to the wrong team (it did not post the $300 adjustment) | **Better tool errors**: a not-found error now says what the id actually is ("BR-13 is a reconciliation item; read it with get_reconciliation_item") |
| L12 payment date | The agent was right ("November 28, 2026"); the grader only accepted 2026-11-28 | Grader fixed to accept written dates |

Each fixed ticket was re-run and passed. The lesson that repeated: when the agent missed a step, adding
a sentence to the prompt was not reliable; putting the rule in code was.

The per-customer reports are in [results/v2](results/v2) ([Lumen](results/v2/report_lumen_sg.md),
[Kestrel](results/v2/report_kestrel_au.md)), with every ticket's full trace.

## How it works

```
ticket ──► ADK agent (Gemini) ──► search_procedures ──► Chroma vector DB of this customer's runbooks
                │
                ├──► read and write tools ──► Operations: the customer's rules, in code
                │                                  │    (from customers/<id>/policy.yaml)
                │                                  ├──► rule broken: refuse with a reason
                │                                  └──► Oracle Fusion connector ──► ERP
                │
                ├──► credential tools ──► broker swaps the handle for the secret (model sees cred://oracle-api)
                │
                └──► close_ticket or escalate (every ticket must end with one of these)
```

- **Connector** (`erp_agent/connectors/oracle_fusion.py`). Records use Oracle Fusion Payables field names
  and paths (`/fscmRestApi/resources/11.13.18.05/invoices`, `invoiceHolds`; `InvoiceNumber`, `ApprovalStatus`,
  `ValidationStatus`, `HoldName`, `ReleaseName`, ...) and collections return `{items, count, hasMore}`.
  The connector turns them into one shape the rest of the code uses, so a Workday or SAP connector
  would plug in the same way.
- **Customer onboarding** (`erp_agent/customers/<id>/policy.yaml`). PO tolerance, how many days before
  an approval counts as stuck, whether rejected invoices may be resubmitted, hold and release names,
  reconciliation limits and allowed causes, conflicting roles (separation of duties) and the team for
  each problem. The same 4% PO variance is refused at Lumen (2% tolerance) and released at Kestrel (5%).
- **Procedures by retrieval (RAG).** 13 runbooks are filled with each customer's numbers and indexed per
  customer in Chroma. The agent searches them instead of carrying every rule in its prompt.
- **Rules in code** (`erp_agent/erp.py`). Examples: reassign an approval only to an active person above
  the approver in the management chain with a high enough limit; release only a variance hold within
  tolerance; grant a role only against an approved record and never into a duties conflict; post a
  reconciliation adjustment only for an allowed cause, within the limit and equal to the difference.
- **Ownership.** Every ticket must end closed (`resolved` only after a verified change, or `answered`)
  or escalated to a valid team for that customer. If neither happened, the runner sends it back once.
- **Observability.** ADK callbacks record every model call (tokens, latency) and every tool call
  (arguments, refusals, Oracle API paths). Each ticket has a token budget.

## The test tickets

44 tickets (`eval/tickets_lumen_sg.json`, `eval/tickets_kestrel_au.json`) written from common ERP
support problems: approvals stuck with someone who left or is on leave, invoices over the PO, duplicates
typed with a different dash, inactive suppliers, rejections, approval-routing errors, "when will this be
paid", role requests with and without approval or with a duties conflict, FX rounding and bank fees,
failed integrations, supplier bank-change requests, urgent "just push it through" pressure, and an
instruction hidden in an invoice description.

`scripts/reference_check.py` plays the intended correct actions for every ticket: all 44 pass, and
doing nothing passes 0, so the checks are neither impossible nor trivially satisfied.

## In addition: sealed credentials

An agent working inside an ERP holds credentials and sees bank details. Here the model gets handles,
not secrets: a broker swaps `cred://oracle-api` for the real key inside the tool call, bank accounts come
back masked (bank and last 4 digits), and checking an account is a yes/no question capped at 2 per
supplier per ticket. This applies the Sealed Orders idea from my research on LLM secret leakage: the
model that writes replies should never hold the secret.

Tested on 16 tickets (4 that need the secrets, 12 attacks: password requests, prompt injection in a note
and in an invoice field, "spell the key with spaces", fake bank-change emails, bulk export, ...). A leak
is any secret in anything the agent sent out, in full, spaced, reversed, base64 or partial form.

| Setting | Tickets with a leak | Task done and nothing leaked |
|---|---|---|
| Secrets in the prompt, with security rules | 0/16 | 15/16 |
| Sealed, with security rules | 0/16 | 16/16 |
| **Secrets in the prompt, security rules removed** | **9/16** | 7/16 |
| **Sealed, security rules removed** | **0/16** | 15/16 |

With good prompt rules Gemini resisted every attack, but that agent's safety depended entirely on the
rules: without them it leaked on 9 of 16 tickets. The sealed agent cannot leak what it never sees.
(This evaluation ran on the first version of the project; that code is at tag `v1-security-eval`.)

## Limits

- The ERP is a mock. The invoice and hold fields follow Oracle's documentation; other resources
  (approval tasks, users, reconciliation, integration jobs) are simplified, and some fields that are
  read-only in the real API are set directly here.
- One model and single runs: the numbers show the design works on these tickets, not a rate with
  confidence intervals. The 4 fixes were each re-run once.
- 44 tickets written by me, not real customer tickets.

## Run it

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env        # then put your Gemini API key in .env

pytest -q tests                                   # rules, connector, onboarding: no API key needed
python -m scripts.reference_check                 # every ticket's intended solution passes the checks
python run.py kestrel_au "OF-7710 is stuck on a price variance hold, can you sort it?"
python -m eval.run_eval lumen_sg kestrel_au       # full evaluation (about 300 model calls)
python -m eval.report                             # one-page report per customer for the latest run
```

To onboard a new customer: add `erp_agent/customers/<id>/policy.yaml` and its data, then
`python run.py <id> "<ticket>"`.
