"""
chain.py — Phase 3 RAG logic module

This module exposes a single public function:

    ask(question: str) -> dict

It implements a six-step Retrieval-Augmented Generation (RAG) flow that
runs entirely on-premise. No data leaves the network.

Six-step flow
-------------
  Step 1 — Embed the question via local Ollama (embeddings API)
  Step 2 — Search the Qdrant "codebase-index" collection, top-5 results
  Step 3 — Optionally query Neo4j for graph relationships
            (triggered when the question contains any of _NEO4J_KEYWORDS)
  Step 4 — Build the locked-in prompt template with retrieved context
  Step 5 — Call Ollama generate (stream=False) to produce the answer
  Step 6 — Return structured response dict

Return schema
-------------
  {
      "answer":             str    — plain English answer from Ollama
      "sources":            list   — top-5 Qdrant results
                                     each: {repo, file, method, lines, score}
      "graph_context_used": bool   — True if Neo4j was queried and returned rows
  }

Environment variables
---------------------
  OLLAMA_HOST      Ollama base URL               (default: http://localhost:11434)
  OLLAMA_MODEL     Ollama model name              (default: deepseek-coder:6.7b)
  OLLAMA_TIMEOUT   Soft timeout warning, seconds  (default: 120)
  QDRANT_HOST      Qdrant hostname                (default: localhost)
  QDRANT_PORT      Qdrant HTTP port               (default: 6333)
  QDRANT_API_KEY   Optional Qdrant API key        (default: empty — not needed on LAN)
  NEO4J_URI        Neo4j bolt URI                 (default: bolt://localhost:7687)
  NEO4J_USER       Neo4j username                 (default: neo4j)
  NEO4J_PASSWORD   Neo4j password                 (required; graph disabled if unset)

Design rules enforced here
--------------------------
  - No LangChain — all AI calls go directly to the local Ollama instance via
    the `ollama` Python client.  No external AI provider is contacted.
  - Neo4j failure is non-fatal: logs a warning, sets graph_context_used=False,
    and continues.  Never crashes because graph context is optional.
  - Qdrant and Ollama failures re-raise so app.py can return HTTP 500.
  - Zero Qdrant results produce a helpful message, not an exception.
  - Clients are built inside ask() per call so the module imports cleanly
    even when services are temporarily down, and env var changes (e.g. in
    tests) take effect immediately without restarting.
"""

import logging
import os
import pathlib
import time
from typing import Optional

import ollama as ollama_lib
from dotenv import load_dotenv
from neo4j import GraphDatabase
from qdrant_client import QdrantClient

# Load infrastructure/.env if present — path resolved relative to this file
# so it works regardless of CWD.  Has no effect in Docker where env vars are
# injected by docker-compose.
load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent.parent / "infrastructure" / ".env")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants — locked in DECISIONS.md, never changed here
# ---------------------------------------------------------------------------

# Qdrant collection name — must match exactly what qdrant_writer.py creates.
_COLLECTION_NAME: str = "codebase-index"

# Top-k results to retrieve from Qdrant per query.
_TOP_K: int = 5

# Soft timeout in seconds.  If Ollama generate takes longer than this,
# a warning is logged.  This is a monitoring aid, not a hard kill —
# a hard async timeout is the responsibility of the calling HTTP layer.
_OLLAMA_TIMEOUT: int = int(os.environ.get("OLLAMA_TIMEOUT", "120"))

# Keywords that trigger a Neo4j graph context lookup in Step 3.
# Any question word (case-insensitive) matching one of these causes Neo4j
# to be queried for service relationships relevant to the question.
_NEO4J_KEYWORDS: frozenset = frozenset(
    {
        "calls",
        "depends",
        "breaks",
        "publisher",
        "consumer",
        "queue",
        "downstream",
        "affect",
        "impact",
    }
)

# Prompt template — must be character-for-character identical to PHASE3-rag.md
# Step 4.  Do not reword this without updating DECISIONS.md Phase 3 section.
_PROMPT_TEMPLATE: str = """\
You are a senior software architect helping developers understand a large codebase.
You have access to the following code context retrieved from the codebase:

{qdrant_context}

{neo4j_context}

Answer the following question clearly and concisely.
Always mention the exact repository name and file path when referring to code.
If you are unsure, say so — do not guess.

Question: {question}"""


# ---------------------------------------------------------------------------
# Private helper — build clients
# ---------------------------------------------------------------------------


def _build_clients() -> tuple[
    ollama_lib.Client,
    QdrantClient,
    str,               # ollama model name resolved from env
    Optional[object],  # Neo4j driver or None
]:
    """
    Instantiate Ollama, Qdrant, and (optionally) Neo4j clients from env vars.

    Returns
    -------
    tuple
        (ollama_client, qdrant_client, ollama_model, neo4j_driver_or_None)

    Raises
    ------
    Exception
        If Qdrant or Ollama client construction fails outright.
        Neo4j failures are swallowed — the driver is returned as None.

    Note: clients are built per ask() call rather than at module import time
    so that (a) the module imports cleanly even when services are down, and
    (b) env var overrides in tests take effect without restarting the process.
    """
    # -- Ollama --
    ollama_host: str = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    ollama_model: str = os.environ.get("OLLAMA_MODEL", "deepseek-coder:6.7b")
    ollama_client = ollama_lib.Client(host=ollama_host)
    logger.debug("Ollama client → %s (model: %s)", ollama_host, ollama_model)

    # -- Qdrant --
    qdrant_host: str = os.environ.get("QDRANT_HOST", "localhost")
    qdrant_port: int = int(os.environ.get("QDRANT_PORT", "6333"))
    qdrant_api_key: Optional[str] = os.environ.get("QDRANT_API_KEY") or None
    qdrant_client = QdrantClient(
        host=qdrant_host,
        port=qdrant_port,
        api_key=qdrant_api_key,
        https=qdrant_api_key is not None,  # only use TLS when a key is configured
    )
    logger.debug("Qdrant client → %s:%d", qdrant_host, qdrant_port)

    # -- Neo4j (optional) --
    neo4j_password: Optional[str] = os.environ.get("NEO4J_PASSWORD")
    neo4j_driver = None

    if not neo4j_password:
        # Graph context disabled — this is a valid configuration for developer
        # machines where Neo4j may not be populated yet.
        logger.info(
            "NEO4J_PASSWORD not set — graph context disabled for this request."
        )
    else:
        neo4j_uri: str = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        neo4j_user: str = os.environ.get("NEO4J_USER", "neo4j")
        try:
            neo4j_driver = GraphDatabase.driver(
                neo4j_uri, auth=(neo4j_user, neo4j_password)
            )
            logger.debug("Neo4j driver → %s (user: %s)", neo4j_uri, neo4j_user)
        except Exception as exc:
            # Non-fatal: log and continue without graph context.
            logger.warning(
                "Failed to create Neo4j driver for %s: %s — "
                "continuing without graph context.",
                neo4j_uri,
                exc,
            )
            neo4j_driver = None

    return ollama_client, qdrant_client, ollama_model, neo4j_driver


# ---------------------------------------------------------------------------
# Private helper — Step 1: embed question
# ---------------------------------------------------------------------------


def _embed_question(
    client: ollama_lib.Client, question: str, model: str
) -> list[float]:
    """
    Produce a vector embedding for the developer's question.

    Uses the same Ollama embeddings API and model that Phase 2 used for
    indexing (qdrant_writer._embed), so the query vector lives in the same
    semantic space as the indexed code vectors.

    Parameters
    ----------
    client : ollama_lib.Client
        Initialised Ollama client.
    question : str
        The raw question string from the developer.
    model : str
        The Ollama model name, e.g. "deepseek-coder:6.7b".

    Returns
    -------
    list[float]
        The embedding vector.

    Raises
    ------
    Exception
        Propagated from Ollama on any failure — Qdrant search is impossible
        without a valid vector, so the caller (ask) should let this propagate
        to app.py which maps it to HTTP 500.
    """
    try:
        # Same API shape used in qdrant_writer._embed:
        # client.embeddings(model=..., prompt=...) → {"embedding": [...]}
        response = client.embeddings(model=model, prompt=question)
        vector: list[float] = response["embedding"]
        logger.debug(
            "Embedded question (%d chars) → vector dim %d", len(question), len(vector)
        )
        return vector
    except Exception as exc:
        logger.error(
            "Ollama embedding failed for question '%.60s…': %s", question, exc
        )
        raise


# ---------------------------------------------------------------------------
# Private helper — Step 2: search Qdrant
# ---------------------------------------------------------------------------


def _search_qdrant(
    client: QdrantClient, vector: list[float]
) -> tuple[list[dict], str]:
    """
    Search the "codebase-index" collection for the top-5 most relevant chunks.

    Parameters
    ----------
    client : QdrantClient
        Connected Qdrant client.
    vector : list[float]
        Embedding vector for the question.

    Returns
    -------
    tuple[list[dict], str]
        sources     — list of dicts suitable for the response "sources" field,
                      each with keys: repo, file, method, lines, score.
        qdrant_ctx  — formatted string of results for the LLM prompt.
                      Returns a "no results" message if the collection is empty.

    Raises
    ------
    Exception
        Propagated on Qdrant connection failure — no answer is possible
        without search results.
    """
    try:
        results = client.search(
            collection_name=_COLLECTION_NAME,
            query_vector=vector,
            limit=_TOP_K,
            with_payload=True,
        )
    except Exception as exc:
        logger.error("Qdrant search failed on collection '%s': %s", _COLLECTION_NAME, exc)
        raise

    if not results:
        # Collection exists but is empty, or vector matched nothing.
        # Return a helpful message so the LLM can explain the situation
        # rather than crashing or giving a nonsensical answer.
        logger.warning(
            "Qdrant search returned 0 results from '%s'. "
            "The collection may not have been indexed yet.",
            _COLLECTION_NAME,
        )
        no_data_msg = (
            "No indexed code was found in the codebase-index collection. "
            "The collection may be empty — run the indexer first "
            "(python indexer/index_repos.py)."
        )
        return [], no_data_msg

    # Build the sources list (public-facing, 5 keys only per spec).
    sources: list[dict] = []
    context_lines: list[str] = []

    for p in results:
        payload = p.payload or {}
        sources.append(
            {
                "repo": payload.get("repo", ""),
                "file": payload.get("file", ""),
                "method": payload.get("method", ""),
                "lines": payload.get("lines", ""),
                "score": round(p.score, 4),
            }
        )
        # Build a human-readable line for the prompt context.
        context_lines.append(
            f"- [{payload.get('repo', '?')}] {payload.get('file', '?')} "
            f"→ {payload.get('method', '?')} "
            f"(lines {payload.get('lines', '?')}): "
            f"{payload.get('summary', 'no summary')}"
        )

    qdrant_context: str = "\n".join(context_lines)
    logger.debug(
        "Qdrant returned %d results from '%s'.", len(sources), _COLLECTION_NAME
    )
    return sources, qdrant_context


# ---------------------------------------------------------------------------
# Private helper — Step 3: query Neo4j
# ---------------------------------------------------------------------------


def _query_neo4j(driver, question: str) -> str:
    """
    Query Neo4j for service relationships relevant to the question.

    Triggered only when the question contains a keyword from _NEO4J_KEYWORDS
    AND a Neo4j driver is available.  If Neo4j fails for any reason, this
    function logs a warning and returns an empty string — it never raises.

    Strategy:
      1. Split the question into lowercase words.
      2. Find all words that are in _NEO4J_KEYWORDS → use as relationship filter.
      3. Also scan for plausible service names: capitalised words and words
         ending in "service" (case-insensitive).
      4. For each candidate keyword/service name, run the Cypher from
         PHASE3-rag.md. Deduplicate results across runs.
      5. Format findings as a relationship list for the LLM prompt.

    Parameters
    ----------
    driver : neo4j.GraphDatabase.driver
        Connected Neo4j driver.
    question : str
        The developer's question.

    Returns
    -------
    str
        Formatted relationship context for the prompt, or "" if no results
        or any error occurred.
    """
    # Identify search terms from the question.
    words = question.split()
    search_terms: list[str] = []

    for word in words:
        clean = word.strip("?.,!:;\"'").lower()
        # Include recognised relationship keywords.
        if clean in _NEO4J_KEYWORDS:
            search_terms.append(clean)
        # Include likely service names (capitalised word or ends with "service").
        if (word[0].isupper() and len(word) > 2) or clean.endswith("service"):
            search_terms.append(word.strip("?.,!:;\"'"))

    # Remove duplicates while preserving order.
    seen: set = set()
    unique_terms: list[str] = []
    for t in search_terms:
        if t.lower() not in seen:
            seen.add(t.lower())
            unique_terms.append(t)

    if not unique_terms:
        # Fall back to using the first noun-like word so we always try
        # at least one Cypher query when Neo4j context was requested.
        for word in words:
            if len(word) > 3:
                unique_terms.append(word.strip("?.,!:;\"'"))
                break

    # Cypher from PHASE3-rag.md — returns source service, relationship type,
    # target node type, and target name for any matching Service node.
    cypher = (
        "MATCH (s:Service)-[r]->(t) "
        "WHERE s.name CONTAINS $keyword "
        "RETURN s.name AS source, type(r) AS relationship, "
        "labels(t)[0] AS target_type, t.name AS target "
        "LIMIT 10"
    )

    rows: list[str] = []
    seen_rows: set = set()

    try:
        with driver.session() as session:
            for term in unique_terms:
                try:
                    result = session.run(cypher, keyword=term)
                    for record in result:
                        row_key = (
                            record["source"],
                            record["relationship"],
                            record["target"],
                        )
                        if row_key not in seen_rows:
                            seen_rows.add(row_key)
                            rows.append(
                                f"- {record['source']} "
                                f"--[{record['relationship']}]--> "
                                f"{record['target_type']}:{record['target']}"
                            )
                except Exception as inner_exc:
                    # A single Cypher run failed — log and try next term.
                    logger.warning(
                        "Neo4j Cypher failed for keyword '%s': %s", term, inner_exc
                    )

    except Exception as exc:
        # Session-level failure (e.g. auth error, connection refused).
        logger.warning(
            "Neo4j query failed: %s — continuing without graph context.", exc
        )
        return ""

    if not rows:
        logger.debug("Neo4j returned no relationships for question: %.60s…", question)
        return ""

    context = "Graph relationships:\n" + "\n".join(rows)
    logger.debug("Neo4j returned %d relationship rows.", len(rows))
    return context


# ---------------------------------------------------------------------------
# Private helper — Step 5: call Ollama generate
# ---------------------------------------------------------------------------


def _call_ollama_generate(
    client: ollama_lib.Client, prompt: str, model: str
) -> str:
    """
    Send the assembled prompt to Ollama and return the generated text.

    Parameters
    ----------
    client : ollama_lib.Client
        Initialised Ollama client pointing at the on-premise server.
    prompt : str
        The fully formatted prompt including Qdrant and Neo4j context.
    model : str
        The Ollama model identifier, e.g. "deepseek-coder:6.7b".

    Returns
    -------
    str
        The generated answer text, stripped of leading/trailing whitespace.

    Raises
    ------
    Exception
        Propagated on any Ollama failure — the caller (ask) lets this reach
        app.py which maps it to HTTP 500 with a descriptive message.

    Note on timeouts:
        The `ollama` Python client does not expose a per-call wall-clock
        timeout parameter.  A hard async timeout should be applied at the
        HTTP handler layer (app.py's asyncio.wait_for or similar).  Here
        we apply a soft warning only — if generation takes longer than
        _OLLAMA_TIMEOUT seconds, we log a warning but do not interrupt.
        num_predict=1024 caps output length to reduce runaway generation.
    """
    t_start = time.time()
    try:
        response = client.generate(
            model=model,
            prompt=prompt,
            stream=False,
            options={"num_predict": 1024},
        )
        elapsed = time.time() - t_start
        if elapsed > _OLLAMA_TIMEOUT:
            logger.warning(
                "Ollama generation took %.1fs — exceeded %ds soft timeout. "
                "Consider reducing context size or adding a hard HTTP timeout "
                "in app.py.",
                elapsed,
                _OLLAMA_TIMEOUT,
            )
        answer: str = response["response"].strip()
        logger.debug(
            "Ollama generated %d chars in %.2fs.", len(answer), elapsed
        )
        return answer
    except Exception as exc:
        logger.error(
            "Ollama generate failed (model=%s, prompt_len=%d): %s",
            model,
            len(prompt),
            exc,
        )
        raise


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ask(question: str) -> dict:
    """
    Answer a developer question about the codebase using the RAG pipeline.

    Runs six steps in sequence:
      1. Embed the question with Ollama.
      2. Search Qdrant "codebase-index" collection, retrieve top-5 chunks.
      3. If the question contains relationship keywords, also query Neo4j.
      4. Build the locked-in prompt template with retrieved context.
      5. Call Ollama generate (stream=False) for the answer.
      6. Return structured response dict.

    Steps 1, 2, and 5 re-raise on failure so app.py can return HTTP 500.
    Step 3 (Neo4j) is non-fatal — failure sets graph_context_used=False.

    Parameters
    ----------
    question : str
        Plain English question from a developer, e.g.
        "Where is the RabbitMQ publisher for payment events?"

    Returns
    -------
    dict with keys:
        answer            (str)   — plain English answer from Ollama
        sources           (list)  — top-5 Qdrant results, each a dict with:
                                    repo, file, method, lines, score
        graph_context_used (bool) — True if Neo4j was queried and rows returned

    Raises
    ------
    Exception
        If Ollama or Qdrant is unreachable.  The caller (app.py /ask handler)
        is responsible for catching this and returning HTTP 500.
    """
    t_total_start = time.time()
    logger.info("ask() called with question: %.80s…", question)

    # Build one fresh set of clients per call.
    ollama_client, qdrant_client, ollama_model, neo4j_driver = _build_clients()

    # ------------------------------------------------------------------
    # Step 1 — Embed the question
    # ------------------------------------------------------------------
    t0 = time.time()
    vector = _embed_question(ollama_client, question, ollama_model)
    logger.info("Step 1 embed: %.2fs", time.time() - t0)

    # ------------------------------------------------------------------
    # Step 2 — Search Qdrant
    # ------------------------------------------------------------------
    t1 = time.time()
    sources, qdrant_context = _search_qdrant(qdrant_client, vector)
    logger.info(
        "Step 2 Qdrant search: %.2fs (%d results)", time.time() - t1, len(sources)
    )

    # ------------------------------------------------------------------
    # Step 3 — Neo4j graph context (conditional)
    # ------------------------------------------------------------------
    # Trigger condition: question contains at least one word from _NEO4J_KEYWORDS.
    question_words = {w.strip("?.,!:;\"'").lower() for w in question.split()}
    neo4j_triggered = bool(question_words & _NEO4J_KEYWORDS)
    neo4j_context: str = ""
    graph_context_used: bool = False

    if neo4j_triggered and neo4j_driver is not None:
        t2 = time.time()
        neo4j_context = _query_neo4j(neo4j_driver, question)
        graph_context_used = bool(neo4j_context)
        logger.info(
            "Step 3 Neo4j: %.2fs (used=%s)", time.time() - t2, graph_context_used
        )
        try:
            neo4j_driver.close()
        except Exception:
            pass  # Best-effort close — non-fatal.
    elif neo4j_triggered and neo4j_driver is None:
        logger.info(
            "Step 3 Neo4j: skipped — trigger keywords found but Neo4j is unavailable."
        )
    else:
        logger.info("Step 3 Neo4j: skipped (no trigger keywords in question).")

    # ------------------------------------------------------------------
    # Step 4 — Build the prompt
    # ------------------------------------------------------------------
    # If Neo4j context is empty, use a neutral placeholder so the prompt
    # template is always fully populated and the LLM never sees "{neo4j_context}".
    neo4j_section = neo4j_context if neo4j_context else "No graph context was requested."

    prompt = _PROMPT_TEMPLATE.format(
        qdrant_context=qdrant_context,
        neo4j_context=neo4j_section,
        question=question,
    )
    logger.info("Step 4 prompt built: %d chars", len(prompt))

    # ------------------------------------------------------------------
    # Step 5 — Call Ollama generate
    # ------------------------------------------------------------------
    t3 = time.time()
    answer = _call_ollama_generate(ollama_client, prompt, ollama_model)
    logger.info("Step 5 Ollama generate: %.2fs", time.time() - t3)

    # ------------------------------------------------------------------
    # Step 6 — Return structured response
    # ------------------------------------------------------------------
    total_elapsed = time.time() - t_total_start
    logger.info("ask() completed in %.2fs", total_elapsed)

    return {
        "answer": answer,
        "sources": sources,
        "graph_context_used": graph_context_used,
    }
