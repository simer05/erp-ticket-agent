"""Procedure search: the ERP runbooks in kb/ are split by section and stored in a
local Chroma vector database, so the agent retrieves the rule it needs instead of
carrying every procedure in its prompt.

Embeddings: ERP_EMBEDDINGS=local (default, Chroma's on-device MiniLM model, no API
calls) or ERP_EMBEDDINGS=gemini (Gemini embedding API, uses GOOGLE_API_KEY)."""
import os
from pathlib import Path

import chromadb
from chromadb import Documents, EmbeddingFunction, Embeddings

KB_DIR = Path(__file__).parent / "kb"
GEMINI_EMBED_MODEL = os.environ.get("ERP_EMBED_MODEL", "gemini-embedding-001")
_collection = None


class GeminiEmbeddings(EmbeddingFunction):
    def __init__(self):
        from google import genai
        self.client = genai.Client()

    def __call__(self, input: Documents) -> Embeddings:
        res = self.client.models.embed_content(model=GEMINI_EMBED_MODEL, contents=list(input))
        return [e.values for e in res.embeddings]


def _embedding_function():
    if os.environ.get("ERP_EMBEDDINGS", "local").lower() == "gemini":
        return GeminiEmbeddings()
    return None  # Chroma default (local MiniLM)


def _chunks():
    exclude = set(os.environ.get("ERP_KB_EXCLUDE", "").split(",")) - {""}
    for path in sorted(KB_DIR.glob("*.md")):
        if path.stem in exclude:
            continue
        text = path.read_text(encoding="utf-8")
        title = text.splitlines()[0].lstrip("# ").strip()
        for i, section in enumerate(text.split("\n## ")):
            body = section if i == 0 else "## " + section
            yield f"{path.stem}#{i}", f"{title}\n{body.strip()}", {"doc": path.stem, "title": title}


def collection():
    global _collection
    if _collection is None:
        client = chromadb.EphemeralClient()
        ef = _embedding_function()
        kwargs = {"embedding_function": ef} if ef else {}
        _collection = client.get_or_create_collection("erp_procedures", **kwargs)
        ids, docs, metas = zip(*_chunks())
        _collection.add(ids=list(ids), documents=list(docs), metadatas=list(metas))
    return _collection


def search(query: str, k: int = 3):
    res = collection().query(query_texts=[query], n_results=k)
    return [
        {"source": m["doc"], "text": d, "distance": round(dist, 3)}
        for d, m, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0])
    ]
