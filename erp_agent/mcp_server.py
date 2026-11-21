"""MCP server: the ERP agent's tools for any MCP client (Claude Desktop, Claude Code, Cursor, ChatGPT and others).

One server process serves one customer. The customer is set when the server starts (ERP_CUSTOMER or
--customer), never by the model, so an assistant connected to Lumen cannot read or change Kestrel.

- Reads go straight to the guarded operations layer (erp.py).
- Writes go through the durable action queue (actions.py). A write the customer's policy marks as
  needing approval waits until an approver listed in the policy approves it with
  `python -m erp_agent.approvals`. There is no approve tool over MCP. A background worker thread then
  runs it, and the business rules are checked again at that moment.
- A write can carry a request_id. Sending the same request_id again returns the first action instead of
  making the change twice, so a client that retries after a dropped connection is safe. Reusing a
  request_id for a different request is refused.

Run:  python -m erp_agent.mcp_server --customer kestrel_au            (stdio, for desktop clients)
      python -m erp_agent.mcp_server --customer kestrel_au --http     (streamable HTTP on 127.0.0.1:8000,
                                                                       no authentication: local use only)
"""
import argparse
import os
import sys
import threading

import anyio
from mcp.server.fastmcp import Context, FastMCP

from .actions import DEFAULT_APPROVAL_REQUIRED, PENDING, WRITE_TOOLS, ActionQueue
from .erp import DEFAULT_CUSTOMER, Operations, list_customers

WORKER_INTERVAL_S = float(os.environ.get("ERP_MCP_WORKER_INTERVAL_S", "2"))


def mcp_policy(policy):
    """Read and check the customer's mcp section. A typo must fail at startup, not let a write skip approval."""
    cfg = policy.get("mcp") or {}
    extra = set(cfg) - {"approval_required", "approvers"}
    if extra:
        raise SystemExit(f"unknown keys in the mcp section: {sorted(extra)}")
    required = cfg.get("approval_required", DEFAULT_APPROVAL_REQUIRED)
    if not isinstance(required, list):
        raise SystemExit("mcp.approval_required must be a list of write tools (use [] for none)")
    unknown = set(required) - WRITE_TOOLS
    if unknown:
        raise SystemExit(f"mcp.approval_required has unknown tools {sorted(unknown)}; write tools: {sorted(WRITE_TOOLS)}")
    approvers = cfg.get("approvers") or []
    if not isinstance(approvers, list) or not all(isinstance(a, str) and a.strip() for a in approvers):
        raise SystemExit("mcp.approvers must be a list of names")
    if required and not approvers:
        raise SystemExit("mcp.approvers must name at least one person when any write needs approval")
    return set(required), set(approvers)


def _client_name(ctx):
    try:
        info = ctx.session.client_params.clientInfo
        return f"assistant:{info.name}"
    except Exception:
        return "assistant"


class Worker(threading.Thread):
    """Runs approved actions. Started once per server process, independent of client sessions."""

    def __init__(self, queue, customer_id, ops, ops_lock, interval=WORKER_INTERVAL_S):
        super().__init__(daemon=True, name=f"erp-worker-{customer_id}")
        self.queue, self.customer_id, self.ops, self.ops_lock = queue, customer_id, ops, ops_lock
        self.interval, self.stop = interval, threading.Event()
        self.errors = 0

    def run_once(self):
        try:
            self.queue.heartbeat(self.customer_id)
            self.queue.recover(self.customer_id)   # actions of a worker that stopped after this one started
            with self.ops_lock:
                return self.queue.drain(self.customer_id, self.ops)
        except Exception as e:   # keep running; a locked database or bad row must not stop all approvals
            self.errors += 1
            print(f"[worker {self.customer_id}] {e!r}", file=sys.stderr, flush=True)
            return []

    def _beat(self):   # own thread, so a slow ERP call holding ops_lock can't make this worker look dead
        while not self.stop.wait(self.interval):
            try:
                self.queue.heartbeat(self.customer_id)
            except Exception as e:
                print(f"[heartbeat {self.customer_id}] {e!r}", file=sys.stderr, flush=True)

    def run(self):
        threading.Thread(target=self._beat, daemon=True).start()
        while not self.stop.is_set():
            self.run_once()
            self.stop.wait(self.interval)


def build_server(customer_id=DEFAULT_CUSTOMER, queue=None, ops=None):
    if customer_id not in list_customers():
        raise SystemExit(f"unknown customer {customer_id}; available: {list_customers()}")
    ops = ops or Operations(customer_id)
    queue = queue or ActionQueue()
    needs_approval, approvers = mcp_policy(ops.policy)
    queue.recover(customer_id)
    ops_lock = threading.RLock()   # the worker thread and tool calls share one Operations object

    mcp = FastMCP(f"erp-{customer_id}", instructions=(
        f"ERP support tools for {ops.policy['customer']}. Reads return records. Writes are checked against the "
        "customer's rules; some wait for a person to approve them. Never tell the user a change was made unless "
        "the action status is 'done'."))

    def _read(fn, *a):
        with ops_lock:
            return fn(*a)

    def _write(tool, args, request_id, requested_by):
        action, created, err = queue.submit(customer_id, tool, args, tool in needs_approval,
                                            idempotency_key=request_id or None, requested_by=requested_by)
        if err:
            return {"error": err}
        if created and action["status"] != PENDING:
            with ops_lock:
                action = queue.execute(customer_id, action["id"], ops)
        return _view(action, duplicate=not created)

    async def write(tool, args, request_id, ctx):
        # Runs in a thread: retries back off with sleep, which must not block other sessions.
        return await anyio.to_thread.run_sync(_write, tool, args, request_id, _client_name(ctx))

    async def read(fn, *a):
        return await anyio.to_thread.run_sync(_read, fn, *a)

    # ---------- reads ----------
    @mcp.tool()
    async def get_invoice(invoice_number: str) -> dict:
        """Read an invoice: supplier, PO, amount, variance vs PO, validation, approval and paid status, holds, approval task."""
        return await read(ops.get_invoice, invoice_number)

    @mcp.tool()
    async def find_possible_duplicates(invoice_number: str) -> dict:
        """Other invoices from the same supplier with the same invoice number, ignoring spaces and dashes."""
        return await read(ops.find_possible_duplicates, invoice_number)

    @mcp.tool()
    async def get_payments(invoice_number: str) -> dict:
        """Payments made on an invoice, or the terms date it will be paid on."""
        return await read(ops.get_payments, invoice_number)

    @mcp.tool()
    async def get_user(person_number: str) -> dict:
        """A person's status, manager, approval limit and ERP roles."""
        return await read(ops.get_user, person_number)

    @mcp.tool()
    async def get_supplier(supplier_number: str) -> dict:
        """A supplier record, including whether it is active."""
        return await read(ops.get_supplier, supplier_number)

    @mcp.tool()
    async def get_purchase_order(po_number: str) -> dict:
        """A purchase order: supplier, amount, status."""
        return await read(ops.get_purchase_order, po_number)

    @mcp.tool()
    async def get_access_approval(approval_id: str) -> dict:
        """An access approval: person, role, approver, status."""
        return await read(ops.get_access_approval, approval_id)

    @mcp.tool()
    async def get_reconciliation_item(item_id: str) -> dict:
        """An unmatched bank reconciliation item with its suggested cause and difference."""
        return await read(ops.get_reconciliation_item, item_id)

    @mcp.tool()
    async def get_integration_job(job_id: str) -> dict:
        """An integration job's status, last run and error."""
        return await read(ops.get_integration_job, job_id)

    @mcp.tool()
    async def search_procedures(query: str) -> dict:
        """Search this customer's ERP runbooks for the rule that applies."""
        def go():
            from . import knowledge   # loads the vector database only when first needed
            return {"results": knowledge.search(customer_id, ops.policy, query)}
        return await anyio.to_thread.run_sync(go)

    # ---------- writes (through the action queue) ----------
    @mcp.tool()
    async def validate_invoice(invoice_number: str, ctx: Context, request_id: str = "") -> dict:
        """Re-run invoice validation: places holds for problems and releases a variance hold now within tolerance."""
        return await write("validate_invoice", {"invoice_number": invoice_number}, request_id, ctx)

    @mcp.tool()
    async def place_hold(invoice_number: str, hold_type: str, reason: str, ctx: Context, request_id: str = "") -> dict:
        """Place a hold (amount_variance, duplicate, supplier_inactive, bank_mismatch) on an unpaid invoice."""
        return await write("place_hold", {"invoice_number": invoice_number, "hold_type": hold_type,
                                          "reason": reason}, request_id, ctx)

    @mcp.tool()
    async def release_hold(invoice_number: str, hold_type: str, reason: str, ctx: Context, request_id: str = "") -> dict:
        """Release an amount-variance hold when the variance is within this customer's tolerance."""
        return await write("release_hold", {"invoice_number": invoice_number, "hold_type": hold_type,
                                            "reason": reason}, request_id, ctx)

    @mcp.tool()
    async def send_approval_reminder(invoice_number: str, ctx: Context, request_id: str = "") -> dict:
        """Remind an active approver about an invoice that has waited longer than this customer allows."""
        return await write("send_approval_reminder", {"invoice_number": invoice_number}, request_id, ctx)

    @mcp.tool()
    async def reassign_approval(invoice_number: str, new_approver: str, reason: str, ctx: Context,
                                request_id: str = "") -> dict:
        """Move an approval from an inactive or on-leave approver to an eligible manager above them."""
        return await write("reassign_approval", {"invoice_number": invoice_number, "new_approver": new_approver,
                                                 "reason": reason}, request_id, ctx)

    @mcp.tool()
    async def resubmit_for_approval(invoice_number: str, new_approver: str, reason: str, ctx: Context,
                                    request_id: str = "") -> dict:
        """Resubmit an invoice rejected only because the approver was unavailable, if this customer allows it."""
        return await write("resubmit_for_approval", {"invoice_number": invoice_number, "new_approver": new_approver,
                                                     "reason": reason}, request_id, ctx)

    @mcp.tool()
    async def grant_role(person_number: str, role: str, approval_id: str, ctx: Context, request_id: str = "") -> dict:
        """Grant an ERP role backed by an approved access request, with separation-of-duties checks."""
        return await write("grant_role", {"person_number": person_number, "role": role,
                                          "approval_id": approval_id}, request_id, ctx)

    @mcp.tool()
    async def post_adjustment(item_id: str, amount: float, reason: str, ctx: Context, request_id: str = "") -> dict:
        """Post a small reconciliation adjustment for a cause and amount this customer allows."""
        return await write("post_adjustment", {"item_id": item_id, "amount": amount, "reason": reason},
                           request_id, ctx)

    @mcp.tool()
    async def escalate(team: str, subject_id: str, summary: str, ctx: Context, request_id: str = "") -> dict:
        """Send the issue to one of this customer's teams."""
        return await write("escalate", {"team": team, "subject_id": subject_id, "summary": summary}, request_id, ctx)

    # ---------- action status ----------
    @mcp.tool()
    async def get_action(action_id: str) -> dict:
        """Status of a write: pending_approval, queued, running, done, refused, rejected, cancelled, failed or
        needs_review (outcome unknown; a person is checking the ERP)."""
        a = await anyio.to_thread.run_sync(queue.get, customer_id, action_id)
        return _view(a) if a else {"error": f"no action {action_id}"}

    @mcp.tool()
    async def list_actions(status: str = "") -> dict:
        """Writes requested for this customer, optionally filtered by status."""
        rows = await anyio.to_thread.run_sync(queue.list, customer_id, status or None)
        return {"actions": [_view(a) for a in rows]}

    @mcp.tool()
    async def cancel_action(action_id: str, ctx: Context) -> dict:
        """Withdraw a write this client requested that is waiting for approval or has not started yet."""
        by = _client_name(ctx)
        a = await anyio.to_thread.run_sync(lambda: queue.cancel(customer_id, action_id, by=by))
        return a if "error" in a else _view(a)

    mcp.ops, mcp.queue, mcp.customer_id, mcp.ops_lock = ops, queue, customer_id, ops_lock
    mcp.worker = Worker(queue, customer_id, ops, ops_lock)
    return mcp


def _view(a, duplicate=False):
    out = {"action_id": a["id"], "tool": a["tool"], "status": a["status"], "attempts": a["attempts"]}
    notes = []
    if duplicate:
        notes.append("This request_id was already used; this is the original action, not a new change.")
    if a["status"] == PENDING:
        notes.append("Waiting for a person to approve. Nothing has changed in the ERP yet.")
    if notes:
        out["note"] = " ".join(notes)
    if a["result"] is not None:
        out["result"] = a["result"]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--customer", default=os.environ.get("ERP_CUSTOMER", DEFAULT_CUSTOMER), choices=list_customers())
    ap.add_argument("--http", action="store_true", help="serve streamable HTTP on 127.0.0.1 (no authentication)")
    a = ap.parse_args()
    server = build_server(a.customer)
    server.worker.start()   # once per process, whatever the transport or number of sessions
    if a.http:
        server.settings.host = "127.0.0.1"
    server.run(transport="streamable-http" if a.http else "stdio")


if __name__ == "__main__":
    main()
