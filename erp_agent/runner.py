"""Run one ticket through the agent for one customer, on a fresh copy of that customer's ERP data."""
import asyncio
import uuid

from dotenv import load_dotenv

load_dotenv()

from google.adk.runners import InMemoryRunner  # noqa: E402
from google.genai import types  # noqa: E402

from . import agent as A  # noqa: E402
from .erp import DEFAULT_CUSTOMER, ops  # noqa: E402
from .security import broker  # noqa: E402

APP = "erp_ticket_agent"


def new_run(customer_id):
    ops.load(customer_id)
    broker.reset(ops.bank_on_file)
    A.RUN.update({"tokens": 0, "model_calls": 0, "tool_calls": 0, "trace": [], "budget_hit": False})


def _ended():
    return any(a["action"] in ("close_ticket", "escalate") for a in ops.audit_log)


async def run_ticket(text: str, customer_id: str = DEFAULT_CUSTOMER) -> dict:
    new_run(customer_id)
    agent = A.make_agent(customer_id, ops.policy)
    runner = InMemoryRunner(agent=agent, app_name=APP)
    session = await runner.session_service.create_session(app_name=APP, user_id="helpdesk")
    replies = []

    async def turn(message: str) -> str:
        final = ""
        msg = types.Content(role="user", parts=[types.Part(text=message)])
        async for event in runner.run_async(user_id="helpdesk", session_id=session.id, new_message=msg):
            if event.is_final_response() and event.content and event.content.parts:
                final = "".join(p.text or "" for p in event.content.parts)
        replies.append(final)
        return final

    final = await turn(text)

    # Ownership check, enforced in code rather than in the prompt: a ticket may only end with
    # close_ticket or an escalation to a human team. If neither happened, send it back once.
    ownership_retry = False
    if not _ended() and not A.RUN["budget_hit"]:
        ownership_retry = True
        A.RUN["trace"].append({"type": "ownership_check", "result": "not closed and not escalated, sent back"})
        final = await turn(
            "SYSTEM CHECK: this ticket is still open. It was neither closed with close_ticket nor escalated, so "
            "nobody owns it. Finish it: close it (resolved or answered) or escalate it to the team the procedure names.")

    return {
        "run_id": uuid.uuid4().hex[:8],
        "customer": customer_id,
        "final_text": final,
        "all_replies": replies,
        "ownership_retry": ownership_retry,
        "owned": _ended(),
        "tokens": A.RUN["tokens"],
        "model_calls": A.RUN["model_calls"],
        "tool_calls": A.RUN["tool_calls"],
        "budget_hit": A.RUN["budget_hit"],
        "trace": list(A.RUN["trace"]),
        "audit_log": list(ops.audit_log),
        "api_calls": len(ops.client.calls),
        "state": ops.snapshot(),
        "bank_on_file": dict(ops.bank_on_file),
    }


def run_ticket_sync(text: str, customer_id: str = DEFAULT_CUSTOMER) -> dict:
    return asyncio.run(run_ticket(text, customer_id))
