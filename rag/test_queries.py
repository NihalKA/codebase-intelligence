"""
test_queries.py — Phase 3 verification script.

Runs the four mandated test queries against the RAG chain and prints a
structured PASS/FAIL report.  Calls chain.ask() in-process — no HTTP server
required.  All backing services (Qdrant, Ollama) must be running.
Neo4j is optional; chain.py degrades gracefully when NEO4J_PASSWORD is unset.

A query PASSES when both conditions hold:
  1. result["answer"] is a non-empty, non-whitespace string
  2. len(result["sources"]) >= 1

Run from repo root:     python -m rag.test_queries
                        python rag/test_queries.py
Run from rag/ folder:   python test_queries.py
Services required:      Qdrant, Ollama (running); Neo4j optional
"""

import pathlib
import sys
import time

# load_dotenv() must be called before importing chain so that environment
# variables set in infrastructure/.env are visible to chain._build_clients().
# Path is resolved relative to this file so it works regardless of CWD.
from dotenv import load_dotenv

load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent.parent / "infrastructure" / ".env")

# ---------------------------------------------------------------------------
# sys.path fallback — makes the script runnable from inside the rag/ folder
# (python test_queries.py) as well as from the repo root
# (python -m rag.test_queries or python rag/test_queries.py).
# When run from inside rag/, the parent directory is not on sys.path, so
# "from rag.chain import ask" would fail without this adjustment.
# ---------------------------------------------------------------------------
import os as _os

_repo_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from rag.chain import ask  # noqa: E402 — must come after sys.path fix

# ---------------------------------------------------------------------------
# Mandated test queries — exact text from PHASE3-rag.md, in order.
# Q4 contains the keyword "affect" which triggers Neo4j context lookup in
# chain._NEO4J_KEYWORDS; the others are Qdrant-only paths.
# ---------------------------------------------------------------------------
_QUERIES: list[str] = [
    "Where is the RabbitMQ publisher for payment events?",
    "How does the payment flow work?",
    "What API endpoints does the order service expose?",
    "If I change PaymentService, what other services might be affected?",
]

_DIVIDER_THIN: str = "─" * 60
_DIVIDER_THICK: str = "═" * 60


def _run_queries() -> int:
    """
    Execute all queries in _QUERIES, print per-query reports, print a summary,
    and return the number of failures (0 = all passed).

    Returns
    -------
    int
        Number of queries that failed.  Callers use this as the exit code
        basis: 0 failures → sys.exit(0), else sys.exit(1).
    """
    total = len(_QUERIES)
    failures: list[tuple[int, str]] = []  # (1-based index, question)

    for idx, question in enumerate(_QUERIES, start=1):
        print(_DIVIDER_THIN)
        print(f"Query {idx}/{total}")
        print(f"Q: {question}")

        result = None
        exc_info = None
        t0 = time.time()

        try:
            result = ask(question)
        except Exception as exc:
            # Catch individually so remaining queries still run.
            exc_info = exc

        elapsed = time.time() - t0
        print(f"Time: {elapsed:.2f}s")

        if exc_info is not None:
            # ask() raised — count as failure but continue.
            print(f"ERROR: {exc_info}")
            print("Result: FAIL")
            failures.append((idx, question))
            continue

        # Evaluate pass/fail criteria.
        answer_text: str = result.get("answer", "")
        sources: list = result.get("sources", [])
        graph_used: bool = result.get("graph_context_used", False)

        answer_ok = bool(answer_text and answer_text.strip())
        sources_ok = len(sources) >= 1
        passed = answer_ok and sources_ok

        # Indent each line of the answer preview with two spaces.
        preview = answer_text[:300] if answer_text else "(empty)"
        indented_preview = "\n".join(f"  {line}" for line in preview.splitlines())

        print("Answer (first 300 chars):")
        print(indented_preview)
        print(f"Sources: {len(sources)}")
        print(f"Graph context used: {graph_used}")
        print(f"Result: {'PASS' if passed else 'FAIL'}")

        if not passed:
            failures.append((idx, question))

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print(_DIVIDER_THICK)
    passes = total - len(failures)

    if not failures:
        print(f"SUMMARY: {passes}/{total} passed")
    else:
        print(f"SUMMARY: {passes}/{total} passed — {len(failures)} FAILED")
        for fail_idx, fail_q in failures:
            print(f'  \u2717 Query {fail_idx}: "{fail_q}"')

    return len(failures)


if __name__ == "__main__":
    failure_count = _run_queries()
    # Print exit code explicitly so CI logs are unambiguous.
    print(f"Exit code: {0 if failure_count == 0 else 1}")
    sys.exit(0 if failure_count == 0 else 1)
