"""
app.py — Phase 3 FastAPI server for the Codebase Intelligence Q&A API.

This module is the HTTP adapter layer.  All RAG logic lives in chain.py.
This file only does:
  - Deserialise HTTP requests into Python types
  - Call chain.ask()
  - Serialise results back to HTTP responses
  - Probe services for /health and /stats without touching RAG logic

Four endpoints:
  POST /ask                    — Answer a developer question (main endpoint)
  GET  /health                 — Liveness/readiness probe for all backing services
  GET  /stats                  — Qdrant vector count + Neo4j node counts by type
  POST /v1/chat/completions    — OpenAI-compatible wrapper for Open WebUI

Environment variables used (all read inside request handlers, not at import time
so the module imports cleanly even when services are down):
  QDRANT_HOST      Qdrant hostname              (default: localhost)
  QDRANT_PORT      Qdrant HTTP port             (default: 6333)
  QDRANT_API_KEY   Optional Qdrant API key      (default: empty — not needed on LAN)
  NEO4J_URI        Neo4j bolt URI               (default: bolt://localhost:7687)
  NEO4J_USER       Neo4j username               (default: neo4j)
  NEO4J_PASSWORD   Neo4j password               (required; graph disabled if unset)
  OLLAMA_HOST      Ollama base URL              (default: http://localhost:11434)
  OLLAMA_MODEL     Ollama model name            (default: deepseek-coder:6.7b)

Run from repo root:
  uvicorn rag.app:app --host 0.0.0.0 --port 8000 --reload

Run from inside rag/ folder:
  uvicorn app:app --host 0.0.0.0 --port 8000 --reload
"""

import logging
import os
import pathlib
import time
from typing import Optional

import ollama as ollama_lib
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from neo4j import GraphDatabase
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient

# ---------------------------------------------------------------------------
# Bootstrap — load env BEFORE importing chain so env vars are visible when
# chain._build_clients() reads them.  In Docker this is a no-op.
# ---------------------------------------------------------------------------
load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent.parent / "infrastructure" / ".env")

# Import the only business logic function this server exposes.
# chain.py must be importable without services running — it builds clients
# lazily inside ask() for exactly this reason.
from rag.chain import ask  # noqa: E402 — must come after load_dotenv

logger = logging.getLogger("rag.app")

# Qdrant collection name — locked in DECISIONS.md, must match qdrant_writer.py.
_COLLECTION_NAME: str = "codebase-index"

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Codebase Intelligence API",
    version="1.0.0",
    description="On-premise RAG API for querying 200+ Java/.NET repositories.",
)

# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------


class AskRequest(BaseModel):
    """Incoming question from a developer."""

    question: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="Plain English question about the codebase.",
    )


class AskResponse(BaseModel):
    """Structured answer returned by the RAG chain."""

    answer: str
    sources: list
    graph_context_used: bool


# --- Open WebUI / OpenAI-compatible types ---


class ChatMessage(BaseModel):
    """Single message in a chat conversation (role + content)."""

    role: str
    content: str


class ChatRequest(BaseModel):
    """OpenAI-compatible chat completion request (used by Open WebUI)."""

    model: str = "deepseek-coder:6.7b"
    messages: list[ChatMessage]
    stream: bool = False


class ChatChoice(BaseModel):
    """Single choice in an OpenAI-compatible completion response."""

    index: int
    message: ChatMessage
    finish_reason: str


class ChatResponse(BaseModel):
    """OpenAI-compatible chat completion response shape."""

    id: str
    object: str
    created: int
    model: str
    choices: list[ChatChoice]


# ---------------------------------------------------------------------------
# Helper — build Qdrant client from env vars
# ---------------------------------------------------------------------------


def _qdrant_client() -> QdrantClient:
    """
    Instantiate a QdrantClient from environment variables.

    Returns
    -------
    QdrantClient
        Configured client pointing at QDRANT_HOST:QDRANT_PORT.

    Note: called inside request handlers, not at module level, so the module
    imports cleanly even when Qdrant is not yet running.
    """
    host: str = os.environ.get("QDRANT_HOST", "localhost")
    port: int = int(os.environ.get("QDRANT_PORT", "6333"))
    api_key: Optional[str] = os.environ.get("QDRANT_API_KEY") or None
    return QdrantClient(host=host, port=port, api_key=api_key)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/ask", response_model=AskResponse)
async def ask_endpoint(request: AskRequest) -> AskResponse:
    """Answer a developer question using the RAG pipeline in chain.py."""
    t0 = time.time()
    try:
        result = ask(request.question)
    except Exception as exc:
        # chain.ask() re-raises on Qdrant or Ollama failure — surface as 500.
        logger.error("/ask failed for question=%.80s… : %s", request.question, exc)
        raise HTTPException(status_code=500, detail=str(exc))

    logger.info("/ask completed in %.2fs", time.time() - t0)
    return AskResponse(
        answer=result["answer"],
        sources=result["sources"],
        graph_context_used=result["graph_context_used"],
    )


@app.get("/health")
async def health() -> dict:
    """
    Probe Qdrant, Neo4j, and Ollama and return their individual statuses.

    Always returns HTTP 200.  Callers inspect each service status field
    independently.  Possible values per service: "ok", "fail", "disabled".
    "disabled" means a required credential (e.g. NEO4J_PASSWORD) is absent —
    the service is intentionally not contacted.
    """
    statuses: dict = {}

    # -- Qdrant --
    try:
        _qdrant_client().get_collections()
        statuses["qdrant"] = "ok"
    except Exception as exc:
        logger.warning("Health check — Qdrant unreachable: %s", exc)
        statuses["qdrant"] = "fail"

    # -- Neo4j --
    neo4j_password: Optional[str] = os.environ.get("NEO4J_PASSWORD")
    if not neo4j_password:
        # Password absent means graph is intentionally disabled for this deployment.
        statuses["neo4j"] = "disabled"
    else:
        neo4j_uri: str = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        neo4j_user: str = os.environ.get("NEO4J_USER", "neo4j")
        driver = None
        try:
            driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))
            driver.verify_connectivity()
            statuses["neo4j"] = "ok"
        except Exception as exc:
            logger.warning("Health check — Neo4j unreachable: %s", exc)
            statuses["neo4j"] = "fail"
        finally:
            if driver is not None:
                driver.close()

    # -- Ollama --
    try:
        ollama_host: str = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        ollama_lib.Client(host=ollama_host).list()
        statuses["ollama"] = "ok"
    except Exception as exc:
        logger.warning("Health check — Ollama unreachable: %s", exc)
        statuses["ollama"] = "fail"

    return statuses


@app.get("/stats")
async def stats() -> dict:
    """
    Return Qdrant vector count for the codebase-index collection and Neo4j
    node counts grouped by node type.

    Fields returned:
      collection    — always "codebase-index"
      qdrant_vectors — integer, or "unavailable" if Qdrant is unreachable
      neo4j_nodes    — dict {label: count}, "disabled", or "unavailable"
    """
    result: dict = {"collection": _COLLECTION_NAME}

    # -- Qdrant stats --
    try:
        client = _qdrant_client()
        info = client.get_collection(_COLLECTION_NAME)
        # vectors_count may be None on versions that report points_count instead.
        vector_count = getattr(info, "vectors_count", None)
        if vector_count is None:
            vector_count = getattr(info, "points_count", "unknown")
        result["qdrant_vectors"] = vector_count
    except Exception as exc:
        logger.warning("Stats — Qdrant unavailable: %s", exc)
        result["qdrant_vectors"] = "unavailable"

    # -- Neo4j stats --
    neo4j_password: Optional[str] = os.environ.get("NEO4J_PASSWORD")
    if not neo4j_password:
        result["neo4j_nodes"] = "disabled"
    else:
        neo4j_uri: str = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        neo4j_user: str = os.environ.get("NEO4J_USER", "neo4j")
        driver = None
        try:
            driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))
            # Count nodes by label — covers Service, Queue, Endpoint, Database
            # as defined in DECISIONS.md Neo4j node types.
            with driver.session() as session:
                records = session.run(
                    "MATCH (n) "
                    "WHERE labels(n)[0] IS NOT NULL "
                    "RETURN labels(n)[0] AS label, count(n) AS cnt"
                )
                node_counts: dict = {r["label"]: r["cnt"] for r in records}
            result["neo4j_nodes"] = node_counts
        except Exception as exc:
            logger.warning("Stats — Neo4j unavailable: %s", exc)
            result["neo4j_nodes"] = "unavailable"
        finally:
            if driver is not None:
                driver.close()

    return result


@app.get("/v1/models")
async def list_models() -> dict:
    """OpenAI-compatible model list endpoint required by Open WebUI model discovery."""
    return {
        "object": "list",
        "data": [
            {
                "id": "codebase",
                "object": "model",
                "created": 1700000000,
                "owned_by": "codebase-intelligence",
            }
        ],
    }


@app.post("/v1/chat/completions", response_model=ChatResponse)
async def openai_compat(request: ChatRequest) -> ChatResponse:
    """
    OpenAI-compatible chat completion endpoint for Open WebUI integration.

    Extracts the last user message from the conversation, passes it to
    chain.ask(), and wraps the answer in an OpenAI-style response shape.
    Streaming is not supported — if stream=True is sent, a warning is logged
    and a standard (non-streamed) response is returned.
    """
    if request.stream:
        # Streaming is out of scope for Phase 3.  Log and fall through to
        # standard response — Open WebUI degrades gracefully on non-streamed
        # replies.
        logger.warning(
            "/v1/chat/completions received stream=True — streaming is not "
            "supported; returning a standard (non-streamed) response."
        )

    # Extract the last user message from the conversation history.
    # Reversed iteration picks the most recent user turn without slicing.
    question: str = next(
        (m.content for m in reversed(request.messages) if m.role == "user"),
        "",
    )

    try:
        result = ask(question)
    except Exception as exc:
        logger.error("/v1/chat/completions ask() raised: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    created_ts = int(time.time())
    # Build rich content — answer + sources block so Open WebUI shows the
    # same evidence trail as the bash demo script.
    content = result["answer"]

    sources = result.get("sources", [])
    if sources:
        content += "\n\n**Sources:**"
        for s in sources:
            content += f"\n- `{s['repo']}` → `{s['file']}` (lines {s['lines']})"

    if result.get("graph_context_used"):
        content += "\n\n_Service dependency graph also used._"

    return ChatResponse(
        id=f"chatcmpl-{created_ts}",
        object="chat.completion",
        created=created_ts,
        model=request.model,
        choices=[
            ChatChoice(
                index=0,
                message=ChatMessage(
                    role="assistant",
                    content=content,
                ),
                finish_reason="stop",
            )
        ],
    )
