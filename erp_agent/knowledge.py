"""Procedure search over the customer's runbooks.

Runbooks in kb/ are templates. When the agent is deployed at a customer, each runbook is filled in
with that customer's policy (tolerances, limits, team names), split by section and stored in a
local Chroma vector database. The agent retrieves the rule it needs instead of carrying every
procedure in its prompt, and it always retrieves this customer's numbers.

Embeddings: ERP_EMBEDDINGS=local (default, Chroma's on-device MiniLM model) or
ERP_EMBEDDINGS=gemini (Gemini embedding API, uses GOOGLE_API_KEY).
"""
import os
from pathlib import Path

import chromadb
from chromadb import Documents, EmbeddingFunction, Embeddings

KB_DIR = Path(__file__).parent / "kb"
GEMINI_EMBED_MODEL = os.environ.get("ERP_EMBED_MODEL", "gemini-embedding-001")
_client = None
_collections = {}


class GeminiEmbeddings(EmbeddingFunction):
    def __init__(self):
        from google import genai
        self.client = genai.Client()

    def __call__(self, input: Documents) -> Embeddings:
        res = self.client.models.embed_content(model=GEMINI_EMBED_MODEL, contents=list(input))
        return [e.values for e in res.embeddings]


def _context(policy):
    ctx = dict(policy)
    if policy["approvals"]["agent_may_resubmit_rejected"]:
        ctx["resubmit_rule"] = (
            "If the approver rejected it only because they were unavailable (reason code APPROVER_UNAVAILABLE), "
            "resubmit it to a different ACTIVE approver whose approval limit covers the amount (start with the "
            "rejecting approver's manager), then close the ticket as resolved.")
    else:
        ctx["resubmit_rule"] = (
            "The agent never resubmits rejected invoices here, whatever the reason. Escalate to accounts payable "
            f"({policy['teams']['accounts_payable']}) with the rejection comment and reason code.")
    ctx["conflict_text"] = "; ".join(" with ".join(p) for p in policy["access"]["conflicting_roles"])
    ctx["causes_text"] = ", ".join(policy["reconciliation"]["auto_adjust_causes"])
    return ctx


def render(policy):
    """Yield (id, text, metadata) for every runbook section, filled in for this customer."""
    exclude = set(os.environ.get("ERP_KB_EXCLUDE", "").split(",")) - {""}
    ctx = _context(policy)
    for path in sorted(KB_DIR.glob("*.md")):
        if path.stem in exclude:
            continue
        text = path.read_text(encoding="utf-8").format_map(ctx)
        title = text.splitlines()[0].lstrip("# ").strip()
        for i, section in enumerate(text.split("\n## ")):
            body = section if i == 0 else "## " + section
            yield f"{path.stem}#{i}", f"{title}\n{body.strip()}", {"doc": path.stem, "title": title}


def collection(customer_id, policy):
    global _client
    if customer_id not in _collections:
        _client = _client or chromadb.EphemeralClient()
        kwargs = {}
        if os.environ.get("ERP_EMBEDDINGS", "local").lower() == "gemini":
            kwargs["embedding_function"] = GeminiEmbeddings()
        col = _client.get_or_create_collection(f"procedures_{customer_id}", **kwargs)
        ids, docs, metas = zip(*render(policy))
        col.upsert(ids=list(ids), documents=list(docs), metadatas=list(metas))
        _collections[customer_id] = col
    return _collections[customer_id]


def search(customer_id, policy, query, k=3):
    res = collection(customer_id, policy).query(query_texts=[query], n_results=k)
    return [
        {"source": m["doc"], "text": d, "distance": round(dist, 3)}
        for d, m, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0])
    ]
