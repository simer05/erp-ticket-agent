"""Run one ticket through the agent with a fresh ERP state."""
import asyncio

from dotenv import load_dotenv

load_dotenv()
import uuid

from google.adk.runners import InMemoryRunner
from google.genai import types

from . import agent as A
from .erp import erp
from .security import broker

APP = "erp_ticket_agent"


def new_run():
    erp.reset()
    broker.reset()
    A.RUN.update({"tokens": 0, "model_calls": 0, "tool_calls": 0, "trace": [], "budget_hit": False})


async def run_ticket(text: str, mode: str = "") -> dict:
    """mode "" = original agent; "naive" or "sealed" = credential-handling variants."""
    new_run()
    agent = A.make_agent(mode) if mode else A.root_agent
    runner = InMemoryRunner(agent=agent, app_name=APP)
    user_id = "helpdesk"
    session = await runner.session_service.create_session(app_name=APP, user_id=user_id)
    replies = []

    async def turn(message: str) -> str:
        final = ""
        msg = types.Content(role="user", parts=[types.Part(text=message)])
        async for event in runner.run_async(user_id=user_id, session_id=session.id, new_message=msg):
            if event.is_final_response() and event.content and event.content.parts:
                final = "".join(p.text or "" for p in event.content.parts)
        replies.append(final)
        return final

    final = await turn(text)

    # Ownership check, enforced in code rather than in the prompt: a ticket may only end with a
    # verified ERP change or an escalation to a human team. If neither happened, send it back once.
    ownership_retry = False
    if not erp.audit_log and not A.RUN["budget_hit"]:
        ownership_retry = True
        A.RUN["trace"].append({"type": "ownership_check", "result": "no change and no escalation, sent back"})
        final = await turn(
            "SYSTEM CHECK: this ticket cannot be closed. No ERP change was made and no team was escalated to, "
            "so nobody owns it. Complete it through the tools, or escalate it to the team the procedure names."
        )
    owned = bool(erp.audit_log)

    return {
        "run_id": uuid.uuid4().hex[:8],
        "final_text": final,
        "all_replies": replies,
        "ownership_retry": ownership_retry,
        "owned": owned,
        "tokens": A.RUN["tokens"],
        "model_calls": A.RUN["model_calls"],
        "tool_calls": A.RUN["tool_calls"],
        "budget_hit": A.RUN["budget_hit"],
        "trace": list(A.RUN["trace"]),
        "audit_log": list(erp.audit_log),
        "state": erp.snapshot(),
        "vendor_bank": dict(broker.bank),
    }


def run_ticket_sync(text: str, mode: str = "") -> dict:
    return asyncio.run(run_ticket(text, mode))
