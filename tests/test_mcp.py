"""Offline checks of the MCP server and the durable action queue (no API key needed).

Covers: tools over the real MCP protocol, human approval before risky writes, per-customer approval
config (and config typos failing at startup), rules re-checked after approval, idempotent request ids,
retries on faults injected in the mock ERP, no retry on unknown outcomes, crash recovery with worker
leases, two processes racing on one action, the background worker, and isolation between customers."""
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import Implementation

from erp_agent import approvals
from erp_agent.actions import (CANCELLED, DONE, FAILED, NEEDS_REVIEW, PENDING, QUEUED, REFUSED, REJECTED, RUNNING,
                               ActionQueue)
from erp_agent.erp import Operations
from erp_agent.mcp_server import build_server, mcp_policy

APPROVERS = {"AP lead", "Finance controller"}


@pytest.fixture
def db(tmp_path):
    return tmp_path / "actions.db"


@pytest.fixture
def queue(db):
    return ActionQueue(db, sleep=lambda s: None)


def call(server, tool, client="test-client", **args):
    """Call one tool through an in-memory MCP client session and return its JSON result."""
    async def go():
        async with create_connected_server_and_client_session(
                server, client_info=Implementation(name=client, version="1")) as session:
            res = await session.call_tool(tool, args)
            assert not res.isError, res.content
            return json.loads(res.content[0].text)
    return asyncio.run(go())


def tool_names(server):
    async def go():
        async with create_connected_server_and_client_session(server) as session:
            return {t.name for t in (await session.list_tools()).tools}
    return asyncio.run(go())


GRANT = dict(person_number="P1005", role="AP_CLERK", approval_id="APR-77")


# ---------- protocol and approval ----------
def test_tools_are_listed_and_there_is_no_approve_tool(queue):
    names = tool_names(build_server("lumen_sg", queue))
    assert {"get_invoice", "release_hold", "grant_role", "get_action", "list_actions", "cancel_action"} <= names
    assert not [n for n in names if "approve" in n and n != "send_approval_reminder" and "reassign" not in n
                and "resubmit" not in n]


def test_read_over_mcp(queue):
    inv = call(build_server("lumen_sg", queue), "get_invoice", invoice_number="NW-2210")
    assert inv["invoice_id"] == 300101 and inv["variance_vs_po_pct"] == 4.0


def test_risky_write_waits_for_an_approver(queue):
    s = build_server("lumen_sg", queue)
    r = call(s, "grant_role", client="claude-desktop", **GRANT)
    assert r["status"] == PENDING
    assert "AP_CLERK" not in s.ops.get_user("P1005")["erp_roles"]          # nothing changed yet
    assert queue.get("lumen_sg", r["action_id"])["requested_by"] == "assistant:claude-desktop"
    assert "error" in queue.approve("lumen_sg", r["action_id"], "assistant:claude-desktop", APPROVERS)
    assert "error" in queue.approve("lumen_sg", r["action_id"], "Assistant", APPROVERS)   # not a listed approver
    assert queue.approve("lumen_sg", r["action_id"], "AP lead", APPROVERS)["status"] == QUEUED
    s.worker.run_once()
    done = call(s, "get_action", action_id=r["action_id"])
    assert done["status"] == DONE and "AP_CLERK" in s.ops.get_user("P1005")["erp_roles"]
    events = [e["event"] for e in queue.audit_trail("lumen_sg", r["action_id"])]
    assert events == ["requested", "approve_denied", "approve_denied", "approve", "claim", "attempt_ok", "done"]


def test_rules_are_checked_again_when_the_approved_action_runs(queue):
    s = build_server("lumen_sg", queue)
    r = call(s, "grant_role", **GRANT)            # P1005 / AP_CLERK would be allowed right now
    s.ops.erp.set_roles("P1005", ["AP_APPROVER"])  # meanwhile someone gives P1005 a conflicting role
    queue.approve("lumen_sg", r["action_id"], "AP lead", APPROVERS)
    s.worker.run_once()
    a = queue.get("lumen_sg", r["action_id"])
    assert a["status"] == REFUSED and "separation of duties" in a["result"]["refused"]


def test_reject_and_cancel(queue):
    s = build_server("lumen_sg", queue)
    r1 = call(s, "grant_role", **GRANT)
    assert queue.reject("lumen_sg", r1["action_id"], "AP lead", APPROVERS, "not needed")["status"] == REJECTED
    assert "error" in queue.approve("lumen_sg", r1["action_id"], "AP lead", APPROVERS)
    r2 = call(s, "post_adjustment", item_id="REC-301", amount=1.38, reason="fx")
    assert call(s, "cancel_action", action_id=r2["action_id"])["status"] == CANCELLED
    assert "error" in queue.approve("lumen_sg", r2["action_id"], "AP lead", APPROVERS)
    s.worker.run_once()
    assert s.ops.get_reconciliation_item("REC-301")["status"] != "MATCHED"


def test_safe_write_runs_now(queue):
    r = call(build_server("lumen_sg", queue), "validate_invoice", invoice_number="NW-2350")
    assert r["status"] == DONE and r["result"]["holds_placed"] == ["Amount"]


def test_approval_rules_come_from_each_customers_policy(queue, tmp_path):
    k = call(build_server("kestrel_au", queue), "release_hold", invoice_number="OF-7710",
             hold_type="amount_variance", reason="within tolerance")
    assert k["status"] == DONE
    l = call(build_server("lumen_sg", ActionQueue(tmp_path / "l.db")), "release_hold", invoice_number="NW-2210",
             hold_type="amount_variance", reason="supplier says ok")
    assert l["status"] == PENDING


@pytest.mark.parametrize("cfg", [{"approval_required": ["relase_hold"], "approvers": ["x"]},
                                 {"approval_required": None},
                                 {"approval_required": ["grant_role"], "approvers": []},
                                 {"approval_required": ["grant_role"], "approvers": "AP lead"},
                                 {"aproval_required": ["grant_role"], "approvers": ["x"]}])
def test_bad_approval_config_fails_at_startup(cfg):
    with pytest.raises(SystemExit):
        mcp_policy({"mcp": cfg})


# ---------- idempotency ----------
def test_same_request_id_is_not_applied_twice(queue):
    s = build_server("lumen_sg", queue)
    args = dict(team="ap_team", subject_id="NW-2210", summary="variance above tolerance", request_id="req-1")
    first = call(s, "escalate", **args)
    again = call(s, "escalate", **args)        # escalate is not idempotent in erp.py; the queue makes it so
    assert first["status"] == DONE and first["action_id"] == again["action_id"] and "already used" in again["note"]
    assert len(s.ops.escalations) == 1


def test_request_id_reused_for_a_different_request_is_refused(queue):
    s = build_server("lumen_sg", queue)
    call(s, "escalate", team="ap_team", subject_id="NW-2210", summary="a", request_id="req-2")
    r = call(s, "escalate", team="finance_control", subject_id="REC-302", summary="b", request_id="req-2")
    assert "already used for a different request" in r["error"]
    assert len(s.ops.escalations) == 1


# ---------- retries (faults injected in the mock ERP) ----------
def test_transient_errors_are_retried(queue):
    ops = Operations("kestrel_au")
    ops.client.inject_faults(503, 429)
    a, _, _ = queue.submit("kestrel_au", "grant_role", {"person_number": "E206", "role": "AP_CLERK",
                                                        "approval_id": "AR-501"}, needs_approval=False)
    a = queue.execute("kestrel_au", a["id"], ops)
    assert a["status"] == DONE and a["attempts"] == 3
    assert [c[2] for c in ops.client.calls if c[0] == "PATCH"][:2] == [{"rejected": 503}, {"rejected": 429}]
    assert "AP_CLERK" in ops.get_user("E206")["erp_roles"]


def test_retries_stop_after_the_limit(queue):
    ops = Operations("kestrel_au")
    ops.client.inject_faults(503, 503, 503, 503)
    a, _, _ = queue.submit("kestrel_au", "grant_role", {"person_number": "E206", "role": "AP_CLERK",
                                                        "approval_id": "AR-501"}, needs_approval=False)
    a = queue.execute("kestrel_au", a["id"], ops)
    assert a["status"] == FAILED and a["attempts"] == 3 and "AP_CLERK" not in ops.get_user("E206")["erp_roles"]


def test_unknown_outcome_is_never_retried_and_a_person_resolves_it(queue):
    ops = Operations("kestrel_au")
    calls = []

    def times_out(**kw):
        calls.append(kw)
        raise TimeoutError("no response")
    ops.grant_role = times_out
    a, _, _ = queue.submit("kestrel_au", "grant_role", {"person_number": "E206", "role": "AP_CLERK",
                                                        "approval_id": "AR-501"}, needs_approval=False)
    a = queue.execute("kestrel_au", a["id"], ops)
    assert a["status"] == NEEDS_REVIEW and len(calls) == 1
    assert "error" in queue.resolve("kestrel_au", a["id"], "AP lead", APPROVERS, "done", "")
    assert queue.resolve("kestrel_au", a["id"], "AP lead", APPROVERS, "failed", "role not on user")["status"] == FAILED


# ---------- durability and concurrency ----------
def test_state_survives_restart_and_dead_workers_actions_go_to_review(db):
    q1 = ActionQueue(db)
    pending, _, _ = q1.submit("lumen_sg", "grant_role", dict(GRANT), needs_approval=True)
    stuck, _, _ = q1.submit("lumen_sg", "validate_invoice", {"invoice_number": "NW-2350"}, needs_approval=False)
    q1._cas("lumen_sg", stuck["id"], (QUEUED,), "claim", status=RUNNING, claimed_by="dead-worker")
    q1.db.close()
    q2 = ActionQueue(db)
    build_server("lumen_sg", q2)                   # startup runs recovery
    assert q2.get("lumen_sg", pending["id"])["status"] == PENDING
    assert q2.get("lumen_sg", stuck["id"])["status"] == NEEDS_REVIEW


def test_a_live_workers_running_action_is_not_taken_over(db):
    a_q, b_q = ActionQueue(db), ActionQueue(db)
    act, _, _ = a_q.submit("lumen_sg", "validate_invoice", {"invoice_number": "NW-2350"}, needs_approval=False)
    a_q.heartbeat("lumen_sg")
    a_q._cas("lumen_sg", act["id"], (QUEUED,), "claim", status=RUNNING, claimed_by=a_q.worker_id)
    assert b_q.recover("lumen_sg") == []           # a second server starting up leaves it alone
    assert b_q.get("lumen_sg", act["id"])["status"] == RUNNING


def test_cancel_and_approve_from_two_processes_only_one_wins(db):
    server_q, console_q = ActionQueue(db), ActionQueue(db)
    act, _, _ = server_q.submit("lumen_sg", "grant_role", dict(GRANT), needs_approval=True, requested_by="assistant:x")
    assert server_q.cancel("lumen_sg", act["id"], by="assistant:x")["status"] == CANCELLED
    r = console_q.approve("lumen_sg", act["id"], "AP lead", APPROVERS)
    assert "cannot approve" in r["error"] and console_q.get("lumen_sg", act["id"])["status"] == CANCELLED


def test_two_workers_never_run_the_same_action(db):
    q1, q2 = ActionQueue(db), ActionQueue(db)
    ops = Operations("lumen_sg")
    act, _, _ = q1.submit("lumen_sg", "escalate", {"team": "ap_team", "subject_id": "X", "summary": "s"},
                          needs_approval=False)
    q1.execute("lumen_sg", act["id"], ops)
    q2.execute("lumen_sg", act["id"], ops)         # claim fails: already done
    assert len(ops.escalations) == 1


def test_background_worker_runs_approved_actions(queue):
    s = build_server("lumen_sg", queue)
    s.worker.interval = 0.05
    r = call(s, "grant_role", **GRANT)
    s.worker.start()
    try:
        queue.approve("lumen_sg", r["action_id"], "AP lead", APPROVERS)
        for _ in range(100):
            if queue.get("lumen_sg", r["action_id"])["status"] == DONE:
                break
            time.sleep(0.05)
        assert queue.get("lumen_sg", r["action_id"])["status"] == DONE
    finally:
        s.worker.stop.set()


def test_worker_survives_errors(queue):
    s = build_server("lumen_sg", queue)
    s.worker.queue = None                          # anything that makes a pass blow up
    assert s.worker.run_once() == [] and s.worker.errors == 1


# ---------- isolation ----------
def test_customers_are_isolated(queue):
    s = build_server("lumen_sg", queue)
    r = call(s, "grant_role", **GRANT)
    assert queue.get("kestrel_au", r["action_id"]) is None
    assert "error" in queue.approve("kestrel_au", r["action_id"], "AP lead", APPROVERS)
    k = build_server("kestrel_au", queue)
    assert call(k, "list_actions")["actions"] == []
    assert "error" in call(k, "get_action", action_id=r["action_id"])
    queue.approve("lumen_sg", r["action_id"], "AP lead", APPROVERS)
    with pytest.raises(ValueError):
        queue.execute("lumen_sg", r["action_id"], Operations("kestrel_au"))


def test_approvals_cli(queue, monkeypatch, capsys):
    monkeypatch.setattr(approvals, "ActionQueue", lambda: queue)
    a, _, _ = queue.submit("lumen_sg", "grant_role", dict(GRANT), needs_approval=True)
    assert approvals.main(["list", "--customer", "lumen_sg"]) == 0
    assert a["id"] in capsys.readouterr().out
    assert approvals.main(["approve", a["id"], "--customer", "lumen_sg", "--by", "Someone"]) == 1
    assert approvals.main(["approve", a["id"], "--customer", "lumen_sg", "--by", "AP lead"]) == 0
    assert queue.get("lumen_sg", a["id"])["status"] == QUEUED


# ---------- added after the second review ----------
def test_partly_applied_action_is_not_retried(queue):
    # validate_invoice on NW-2350 places a hold (write 1), then sets the status (write 2). Fail write 2.
    ops = Operations("lumen_sg")
    real_patch = ops.client.patch

    def patch_fails_once(*a, **kw):
        ops.client.patch = real_patch
        ops.client.inject_faults(503)
        return real_patch(*a, **kw)
    ops.client.patch = patch_fails_once
    a, _, _ = queue.submit("lumen_sg", "validate_invoice", {"invoice_number": "NW-2350"}, needs_approval=False)
    a = queue.execute("lumen_sg", a["id"], ops)
    assert a["status"] == NEEDS_REVIEW and a["attempts"] == 1 and "part of this action" in a["result"]["error"]
    holds = [h for h in ops.get_invoice("NW-2350")["open_holds"] if h["hold_name"] == "Amount"]
    assert len(holds) == 1                         # applied once, not again by a retry


def test_worker_pass_recovers_a_worker_that_died_after_startup(db):
    now = [1000.0]
    dead = ActionQueue(db, clock=lambda: now[0])
    act, _, _ = dead.submit("lumen_sg", "validate_invoice", {"invoice_number": "NW-2350"}, needs_approval=False)
    dead.heartbeat("lumen_sg")
    dead._cas("lumen_sg", act["id"], (QUEUED,), "claim", status=RUNNING, claimed_by=dead.worker_id)
    live = ActionQueue(db, clock=lambda: now[0])
    s = build_server("lumen_sg", live)             # restart within the lease: left alone
    assert live.get("lumen_sg", act["id"])["status"] == RUNNING
    now[0] += 31                                   # the dead worker's lease runs out
    s.worker.run_once()
    assert live.get("lumen_sg", act["id"])["status"] == NEEDS_REVIEW


def test_a_client_cannot_cancel_another_clients_action(queue):
    s = build_server("lumen_sg", queue)
    r = call(s, "grant_role", client="cursor", **GRANT)
    assert "only that client can cancel" in call(s, "cancel_action", client="claude-desktop", action_id=r["action_id"])["error"]
    assert call(s, "cancel_action", client="cursor", action_id=r["action_id"])["status"] == CANCELLED


RACE = """
import sys
sys.path.insert(0, sys.argv[3])
from erp_agent.actions import ActionQueue, QUEUED, RUNNING
q = ActionQueue(sys.argv[1])
won = 0
for aid in open(sys.argv[2]).read().split():
    _, err = q._cas("lumen_sg", aid, (QUEUED,), "claim", status=RUNNING, claimed_by=q.worker_id)
    won += err is None
print(won)
"""


def test_two_real_processes_never_claim_the_same_action(db, tmp_path):
    q = ActionQueue(db)
    ids = [q.submit("lumen_sg", "escalate", {"team": "ap_team", "subject_id": str(i), "summary": "s"},
                    needs_approval=False)[0]["id"] for i in range(200)]
    q.db.close()
    (tmp_path / "ids.txt").write_text(" ".join(ids))
    (tmp_path / "race.py").write_text(RACE)
    root = str(Path(__file__).resolve().parent.parent)
    procs = [subprocess.Popen([sys.executable, str(tmp_path / "race.py"), str(db), str(tmp_path / "ids.txt"), root],
                              stdout=subprocess.PIPE, text=True) for _ in range(3)]
    wins = [int(p.communicate(timeout=120)[0]) for p in procs]
    assert sum(wins) == 200                        # every action claimed exactly once across 3 processes
    q2 = ActionQueue(db)
    claims = [e for e in q2.audit_trail("lumen_sg") if e["event"] == "claim"]
    assert len(claims) == 200
