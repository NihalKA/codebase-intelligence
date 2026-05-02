# Phase 3 — RAG Chain and Q&A API
## How to use this file
Attach this file with `#PHASE3-rag.md` in Copilot Chat when using the **Coder** agent.
The Coder agent reads this file to know exactly what to build in this phase.

---

## CONTEXT — read this first

We are building the Q&A layer for a private codebase intelligence platform.

**Read DECISIONS.md Phase 1 AND Phase 2 sections first.**
Use the exact collection names, payload field names, Neo4j node types, and ports already decided.

Stack already running:
- Qdrant on port 6333 with collection "codebase-index"
- Neo4j on port 7687 with Service, Queue, Endpoint nodes
- Ollama on port 11434 with deepseek-coder:6.7b

---

## YOUR GOAL FOR THIS PHASE

Build a LangChain RAG chain that answers developer questions about the codebase,
and expose it as a FastAPI endpoint.

Produce these files:
- `rag/requirements.txt`
- `rag/chain.py` (the RAG logic)
- `rag/app.py` (FastAPI server — includes /ask, /health, /stats, AND /v1/chat/completions for Open WebUI)
- `rag/test_queries.py` (verification script)

---

## DETAILED REQUIREMENTS

### requirements.txt must include:
```
fastapi>=0.109.0
uvicorn>=0.27.0
langchain>=0.1.0
langchain-community>=0.0.20
qdrant-client>=1.7.0
neo4j>=5.0.0
ollama>=0.1.0
python-dotenv>=1.0.0
```

### chain.py — the RAG logic

Build a function `ask(question: str) -> dict` that does this in order:

**Step 1 — Embed the question**
- Call Ollama embed endpoint with the question
- Use the same embedding model used in Phase 2 (read from DECISIONS.md)

**Step 2 — Search Qdrant**
- Search "codebase-index" collection with the question embedding
- Retrieve top 5 results
- Each result includes payload: repo, file, method, lines, language, summary

**Step 3 — Check if Neo4j context is needed**
- If the question contains keywords like: "calls", "depends", "breaks", "publisher", "consumer", "queue", "downstream", "affect", "impact"
- Then also query Neo4j for relevant relationships
- Example Cypher: `MATCH (s:Service)-[r]->(t) WHERE s.name CONTAINS $keyword RETURN s,r,t LIMIT 10`

Note: The Neo4j graph was populated by both:
- neo4j_writer.py (within-file relationships from tree-sitter)
- sourcegraph_client.py (cross-repo relationships from Sourcegraph search)

So when the RAG chain queries Neo4j for CALLS relationships, it will
find connections across all 200 repos — not just within individual files.

**Step 4 — Build the prompt**
Use this template exactly:
```
You are a senior software architect helping developers understand a large codebase.
You have access to the following code context retrieved from the codebase:

{qdrant_context}

{neo4j_context}

Answer the following question clearly and concisely.
Always mention the exact repository name and file path when referring to code.
If you are unsure, say so — do not guess.

Question: {question}
```

**Step 5 — Call Ollama**
- POST to the Ollama host from environment variable OLLAMA_HOST
  (value is http://ollama:11434 in Docker, http://localhost:11434 in local dev)
- Model: from environment variable OLLAMA_MODEL (default: deepseek-coder:6.7b)
- stream: false
- Return the response text

**Step 6 — Return structured response**
```python
return {
    "answer": str,          # plain English answer from Ollama
    "sources": [            # list of Qdrant results used
        {
            "repo": str,
            "file": str,
            "method": str,
            "lines": str,
            "score": float
        }
    ],
    "graph_context_used": bool   # whether Neo4j was queried
}
```

### app.py — FastAPI server

Endpoints required:

**POST /ask**
- Request body: `{ "question": "string" }`
- Response: chain.py ask() output
- Error handling: return 500 with message if Qdrant or Ollama is unreachable

**GET /health**
- Check Qdrant, Neo4j, and Ollama are all reachable
- Return: `{ "qdrant": "ok/fail", "neo4j": "ok/fail", "ollama": "ok/fail" }`

**GET /stats**
- Return Qdrant collection stats (total vectors, collection name)
- Return Neo4j node counts by type

Run with (from repo root): `uvicorn rag.app:app --host 0.0.0.0 --port 8000`
Run with (from inside rag/ folder): `uvicorn app:app --host 0.0.0.0 --port 8000`

### test_queries.py — verification

Run four test queries and print results. The script passes if all four return non-empty answers with at least one source.

Test queries to use:
1. "Where is the RabbitMQ publisher for payment events?"
2. "How does the payment flow work?"
3. "What API endpoints does the order service expose?"
4. "If I change PaymentService, what other services might be affected?"

For each query print:
- The question
- The answer (first 300 chars)
- Number of sources returned
- Whether graph context was used
- PASS or FAIL

---

## HOW TO RUN

```bash
# Start the API server
# Run from repo root
pip install -r rag/requirements.txt
uvicorn rag.app:app --host 0.0.0.0 --port 8000 --reload

# OR run from inside the rag/ folder
cd rag
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000 --reload

# Test the health endpoint
curl http://localhost:8000/health

# Test a question
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "Where is the RabbitMQ publisher for payment events?"}'

# Run all test queries
python test_queries.py
```

---

## WHAT TO WRITE IN DECISIONS.md WHEN DONE

Append to the Phase 3 section:
- FastAPI endpoint URL and format
- Top-k value used for Qdrant retrieval
- Keywords that trigger Neo4j graph lookup
- Which test queries passed and which needed adjustment
- Any prompt template changes and why

---

## CODER AGENT INSTRUCTIONS

- Read DECISIONS.md Phase 1 and Phase 2 before writing any code
- Use the exact collection name, field names, and model name from DECISIONS.md
- All connection details from environment variables
- The chain.py must work standalone (importable) — app.py just wraps it
- Add response time logging — we want to know how long each step takes
- If Ollama is slow, add a timeout and return a helpful error message
- After generating chain.py, walk me through the flow step by step in plain English
- Do not move to app.py until I have tested chain.py works standalone
