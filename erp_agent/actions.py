"""Durable action queue for ERP writes made through the MCP server.

Every write an assistant asks for becomes a row in a SQLite database before anything touches the ERP.

- Human approval. Each customer's policy.yaml lists the writes that need a person
  (`mcp.approval_required`) and the people allowed to approve them (`mcp.approvers`). Those writes wait
  as `pending_approval`. No approve tool is exposed over MCP; approvals are made with
  `python -m erp_agent.approvals` under a name on that list.
- Safe across processes. The approvals console, the server and a second server for the same customer
  are separate processes sharing one database. Every status change is a compare-and-swap inside a
  `BEGIN IMMEDIATE` transaction, with its audit row in the same transaction, so a cancelled action can't
  be approved and run, and two workers can't both run the same action.
- Retries with a clear rule. An action is retried only when the ERP rejected a request before applying
  it (TransientERPError: rate limit, service unavailable) and no earlier write of the same action had
  gone through. Some operations make two or three ERP writes. If one was applied and a later one was
  rejected, re-running the whole operation is not safe, so the action goes to `needs_review`. A timeout
  or any other error might have applied the write, so it also goes to `needs_review`.
- Crash recovery with leases. A worker that runs an action records its id and keeps a heartbeat (in its
  own thread). At startup and on every worker pass, actions left `running` by a worker whose heartbeat
  is older than LEASE_S go to `needs_review`; actions held by a live worker are left alone.
- Audit trail per customer: requests (with the MCP client's self-reported name), decisions, refused
  approvals, request_id replays and conflicts, every attempt with its latency, and the result.

Business rules are still checked by erp.Operations when the action runs, so a write approved earlier is
refused if the record has changed since.
"""
import contextlib
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .errors import TransientERPError  # noqa: F401  (re-exported)

DB_PATH = Path(__file__).resolve().parent.parent / "state" / "actions.db"

WRITE_TOOLS = {"validate_invoice", "place_hold", "release_hold", "send_approval_reminder", "reassign_approval",
               "resubmit_for_approval", "grant_role", "post_adjustment", "escalate"}
# Used when a customer's policy has no mcp section.
DEFAULT_APPROVAL_REQUIRED = ["grant_role", "post_adjustment", "release_hold", "reassign_approval",
                             "resubmit_for_approval"]
MAX_ATTEMPTS = 3
BACKOFF_S = 0.5     # 0.5s, then 1s
LEASE_S = 30        # a worker with no heartbeat for this long is treated as stopped

PENDING, QUEUED, RUNNING = "pending_approval", "queued", "running"
DONE, REFUSED, REJECTED, CANCELLED = "done", "refused", "rejected", "cancelled"
FAILED, NEEDS_REVIEW = "failed", "needs_review"
OPEN = (PENDING, QUEUED)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _args_hash(tool, args):
    return hashlib.sha256(json.dumps([tool, args], sort_keys=True).encode()).hexdigest()


class ActionQueue:
    def __init__(self, db_path=DB_PATH, sleep=time.sleep, clock=time.time):
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()   # one connection per process, shared by its threads
        self.sleep, self.clock = sleep, clock
        self.worker_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS actions (
                id TEXT PRIMARY KEY, tenant TEXT NOT NULL, tool TEXT NOT NULL, args TEXT NOT NULL,
                args_hash TEXT NOT NULL, idempotency_key TEXT NOT NULL, status TEXT NOT NULL,
                requested_by TEXT, decided_by TEXT, decision_note TEXT, claimed_by TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, result TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE (tenant, idempotency_key));
            CREATE TABLE IF NOT EXISTS audit (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, tenant TEXT NOT NULL, action_id TEXT,
                event TEXT NOT NULL, detail TEXT, at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS workers (worker_id TEXT PRIMARY KEY, tenant TEXT, heartbeat REAL NOT NULL);
        """)

    # ---------- transactions ----------
    @contextlib.contextmanager
    def _tx(self):
        """BEGIN IMMEDIATE takes SQLite's write lock up front, so check-and-update is atomic across processes."""
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def _audit(self, tenant, action_id, event, **detail):
        self.db.execute("INSERT INTO audit (tenant, action_id, event, detail, at) VALUES (?,?,?,?,?)",
                        (tenant, action_id, event, json.dumps(detail, default=str), _now()))

    def _cas(self, tenant, action_id, from_statuses, event, audit=None, **fields):
        """Change an action only if it is still in one of from_statuses. Returns (row, error)."""
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        marks = ",".join("?" * len(from_statuses))
        with self._tx():
            cur = self.db.execute(f"UPDATE actions SET {cols} WHERE id = ? AND tenant = ? AND status IN ({marks})",
                                  (*fields.values(), action_id, tenant, *from_statuses))
            if cur.rowcount != 1:
                a = self.get(tenant, action_id)
                return a, (f"no action {action_id} for {tenant}" if not a
                           else f"action {action_id} is {a['status']}; cannot {event}")
            self._audit(tenant, action_id, event, **(audit or {}))
        return self.get(tenant, action_id), None

    # ---------- reads ----------
    @staticmethod
    def _row(r):
        if r is None:
            return None
        d = dict(r)
        d["args"] = json.loads(d["args"])
        d["result"] = json.loads(d["result"]) if d["result"] else None
        return d

    def get(self, tenant, action_id):
        """Rows are always read with the tenant, so one customer can never see another's actions."""
        with self.lock:
            r = self.db.execute("SELECT * FROM actions WHERE id = ? AND tenant = ?", (action_id, tenant)).fetchone()
        return self._row(r)

    def list(self, tenant, statuses=None):
        q, p = "SELECT * FROM actions WHERE tenant = ?", [tenant]
        if statuses:
            statuses = [statuses] if isinstance(statuses, str) else list(statuses)
            q, p = q + f" AND status IN ({','.join('?' * len(statuses))})", p + statuses
        with self.lock:
            rows = self.db.execute(q + " ORDER BY created_at, rowid", p).fetchall()
        return [self._row(r) for r in rows]

    def audit_trail(self, tenant, action_id=None):
        q, p = "SELECT * FROM audit WHERE tenant = ?", [tenant]
        if action_id:
            q, p = q + " AND action_id = ?", p + [action_id]
        with self.lock:
            rows = self.db.execute(q + " ORDER BY seq", p).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"] or "{}")} for r in rows]

    # ---------- requests and decisions ----------
    def submit(self, tenant, tool, args, needs_approval, idempotency_key=None, requested_by="assistant"):
        """Record a write request. Returns (action, created, error).

        The same idempotency key with the same tool and arguments returns the existing action. The same key
        with different arguments is a conflict, not a silent match. Without a key every call is a new action."""
        if tool not in WRITE_TOOLS:
            return None, False, f"{tool} is not a write tool"
        h = _args_hash(tool, args)
        key = idempotency_key or f"auto:{uuid.uuid4().hex}"
        with self._tx():
            old = self.db.execute("SELECT * FROM actions WHERE tenant = ? AND idempotency_key = ?",
                                  (tenant, key)).fetchone()
            if old:
                if old["args_hash"] != h:
                    self._audit(tenant, old["id"], "request_id_conflict", tool=tool, args=args, by=requested_by)
                    return self._row(old), False, (f"request_id {key} was already used for a different request "
                                                   f"({old['tool']} {old['args']}); use a new request_id")
                self._audit(tenant, old["id"], "request_id_replay", by=requested_by)
                return self._row(old), False, None
            aid = "act_" + uuid.uuid4().hex[:12]
            status = PENDING if needs_approval else QUEUED
            now = _now()
            self.db.execute("INSERT INTO actions (id, tenant, tool, args, args_hash, idempotency_key, status, "
                            "requested_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (aid, tenant, tool, json.dumps(args), h, key, status, requested_by, now, now))
            self._audit(tenant, aid, "requested", tool=tool, args=args, by=requested_by, status=status)
        return self.get(tenant, aid), True, None

    def approve(self, tenant, action_id, by, approvers, note=""):
        """approvers: the names allowed to approve at this customer (policy mcp.approvers)."""
        if by not in approvers:
            return self._denied(tenant, action_id, "approve", by)
        row, err = self._cas(tenant, action_id, (PENDING,), "approve", {"by": by, "note": note},
                             status=QUEUED, decided_by=by, decision_note=note)
        return {"error": err} if err else row

    def _denied(self, tenant, action_id, what, by):
        if self.get(tenant, action_id):
            with self._tx():
                self._audit(tenant, action_id, f"{what}_denied", by=by)
        return {"error": f"{by!r} is not an approver at {tenant}"}

    def reject(self, tenant, action_id, by, approvers, note=""):
        if by not in approvers:
            return self._denied(tenant, action_id, "reject", by)
        row, err = self._cas(tenant, action_id, (PENDING,), "reject", {"by": by, "note": note},
                             status=REJECTED, decided_by=by, decision_note=note)
        return {"error": err} if err else row

    def cancel(self, tenant, action_id, by, note="", only_own=True):
        """An assistant may withdraw only the actions it requested."""
        a = self.get(tenant, action_id)
        if a and only_own and a["requested_by"] != by:
            return {"error": f"action {action_id} was requested by {a['requested_by']}; only that client can cancel it"}
        row, err = self._cas(tenant, action_id, OPEN, "cancel", {"by": by, "note": note},
                             status=CANCELLED, decided_by=by, decision_note=note)
        return {"error": err} if err else row

    def resolve(self, tenant, action_id, by, approvers, outcome, note):
        """A person checked the ERP after an unknown outcome and records what actually happened."""
        if by not in approvers:
            return self._denied(tenant, action_id, "resolve", by)
        if outcome not in (DONE, FAILED):
            return {"error": "outcome must be 'done' (the write was applied) or 'failed' (it was not)"}
        if not note:
            return {"error": "say what you checked in the ERP"}
        row, err = self._cas(tenant, action_id, (NEEDS_REVIEW,), "resolve", {"by": by, "outcome": outcome,
                             "note": note}, status=outcome, decided_by=by, decision_note=note)
        return {"error": err} if err else row

    # ---------- workers ----------
    def heartbeat(self, tenant):
        with self._tx():
            self.db.execute("INSERT INTO workers (worker_id, tenant, heartbeat) VALUES (?,?,?) "
                            "ON CONFLICT(worker_id) DO UPDATE SET heartbeat = excluded.heartbeat",
                            (self.worker_id, tenant, self.clock()))

    def _live_workers(self):
        with self.lock:
            rows = self.db.execute("SELECT worker_id FROM workers WHERE heartbeat >= ?",
                                   (self.clock() - LEASE_S,)).fetchall()
        return {r["worker_id"] for r in rows}

    def recover(self, tenant):
        """Actions left `running` by a stopped worker may or may not have reached the ERP. Never re-run them."""
        self.heartbeat(tenant)
        live, out = self._live_workers(), []
        for a in self.list(tenant, RUNNING):
            if a["claimed_by"] in live:
                continue
            row, err = self._cas(tenant, a["id"], (RUNNING,), "recover",
                                 {"worker": a["claimed_by"], "reason": "worker stopped while running"},
                                 status=NEEDS_REVIEW, result=json.dumps(
                                     {"error": "the server stopped while this action was running; check the ERP"}))
            if not err:
                out.append(row)
        return out

    # ---------- execution ----------
    def execute(self, tenant, action_id, ops):
        """Claim one queued action and run it against the tenant's guarded operations."""
        if ops.customer_id != tenant:
            raise ValueError(f"operations are for {ops.customer_id}, action is for {tenant}")
        a, err = self._cas(tenant, action_id, (QUEUED,), "claim", {"worker": self.worker_id},
                           status=RUNNING, claimed_by=self.worker_id)
        if err:
            return a     # someone else claimed, cancelled or finished it
        try:
            return self._run(tenant, a, ops)
        except Exception as e:   # bookkeeping failed after the claim: never leave the action silently running
            try:
                return self._finish(tenant, action_id, NEEDS_REVIEW, {"error": f"worker error after claim: {e!r}"})
            except Exception:
                raise e   # recovery moves it to needs_review once this worker's lease expires

    def _run(self, tenant, a, ops):
        action_id, method, attempts = a["id"], getattr(ops, a["tool"]), a["attempts"]
        client = getattr(ops, "client", None)
        while True:
            attempts += 1
            self.heartbeat(tenant)
            with self._tx():
                self.db.execute("UPDATE actions SET attempts = ? WHERE id = ?", (attempts, action_id))
            writes_before = getattr(client, "writes", None)
            t0 = time.perf_counter()
            try:
                result = method(**a["args"])
            except TransientERPError as e:
                ms = round((time.perf_counter() - t0) * 1000)
                partial = writes_before is None or client.writes != writes_before
                retry = attempts < MAX_ATTEMPTS and not partial
                with self._tx():
                    self._audit(tenant, action_id, "attempt_failed", attempt=attempts, error=str(e),
                                latency_ms=ms, retry=retry, earlier_writes_applied=partial)
                if partial:
                    return self._finish(tenant, action_id, NEEDS_REVIEW, {"error": (
                        f"part of this action was applied before the ERP rejected a later write ({e}); "
                        "a person must check the ERP")})
                if retry:
                    self.sleep(BACKOFF_S * 2 ** (attempts - 1))
                    continue
                return self._finish(tenant, action_id, FAILED,
                                    {"error": f"ERP unavailable after {attempts} attempts: {e}"})
            except Exception as e:  # unknown outcome: the write may or may not have been applied
                ms = round((time.perf_counter() - t0) * 1000)
                with self._tx():
                    self._audit(tenant, action_id, "attempt_unknown", attempt=attempts, error=repr(e), latency_ms=ms)
                return self._finish(tenant, action_id, NEEDS_REVIEW,
                                    {"error": f"outcome unknown ({e!r}); a person must check the ERP"})
            ms = round((time.perf_counter() - t0) * 1000)
            with self._tx():
                self._audit(tenant, action_id, "attempt_ok", attempt=attempts, latency_ms=ms)
            status = REFUSED if ("refused" in result or "error" in result) else DONE
            return self._finish(tenant, action_id, status, result)

    def _finish(self, tenant, action_id, status, result):
        row, err = self._cas(tenant, action_id, (RUNNING,), status, {"result": result},
                             status=status, result=json.dumps(result, default=str))
        if err:   # should not happen: only this worker holds a running action it claimed
            raise RuntimeError(err)
        return row

    def drain(self, tenant, ops):
        """Run every queued action for one tenant, oldest first."""
        return [self.execute(tenant, a["id"], ops) for a in self.list(tenant, QUEUED)]
