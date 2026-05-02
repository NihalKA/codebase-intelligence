"""
rag — Phase 3 RAG chain and Q&A API package.

Public surface:
    rag.chain.ask(question: str) -> dict   — RAG pipeline (embed → search → generate)
    rag.app.app                            — FastAPI application instance

All AI inference stays on-premise via Ollama.  No external AI APIs are used.
"""
