# Architecture — How it works

> This explains the technical architecture in plain English.
> For full component details see `03-component-guide.md`.

---

## The big picture

When a developer asks a question, five things happen in sequence:

```
Developer types question
        ↓
Question is turned into a number (embedding)
        ↓
That number is used to search the knowledge store
        ↓
The most relevant code chunks are retrieved
        ↓
The local AI reads those chunks and writes a plain English answer
        ↓
Developer sees the answer with file references
```

Everything happens inside our network. Nothing leaves.

---

## Four layers

### Layer 1 — Source (your GitHub repos)

Your 200+ repos in GitHub. When code is pushed to main,
a GitHub Actions workflow automatically triggers the indexing pipeline.

---

### Layer 2 — Indexing pipeline (reads and understands the code)

This runs automatically on every code push. It does three things:

**Reading structure (tree-sitter + Sourcegraph)**
Two tools work together to read the code:
- tree-sitter reads each individual file and extracts its structure
  (methods, classes, endpoints, RabbitMQ calls)
- Sourcegraph OSS indexes all repos and provides cross-repo search —
  it can find every caller of a method across all 200 repos instantly
  via its GraphQL API

**Writing summaries (Ollama)**
A local AI model reads each method/class and writes a 2-3 sentence plain English summary.
This is what makes "how does payment flow work?" answerable — you are searching summaries,
not raw code.

**Finding connections (Neo4j writer)**
The pipeline also looks for connections between services:
which services call which, which publish to which queues, which consume from which queues.
These connections are written to the graph database.

---

### Layer 3 — Knowledge store (three databases)

**Qdrant** — the semantic search database

Stores code chunks and their summaries as vectors (lists of numbers).
When you search by meaning ("find RabbitMQ publisher"), Qdrant finds chunks
that mean the same thing, even if they use different words.

**Neo4j** — the relationship database

Stores the connections between services as a graph.
"Service A calls Service B" is one edge in that graph.
This is what powers impact analysis ("what breaks if I change X?").

**MinIO/S3** — the document store

Stores the auto-generated Markdown documentation and architecture diagrams.
Mermaid diagrams are auto-generated from Neo4j relationships (e.g. service call graphs,
queue topologies) and stored alongside the Markdown docs.
All generated docs and diagrams are uploaded to MinIO (`service-docs` bucket),
which acts as the central store. Confluence can sync from MinIO via its REST API,
or docs can be committed directly into the target repository — whichever the team prefers.

---

### Layer 4 — The developer interface

**FastAPI** — the API layer

A lightweight Python server that exposes a single `/ask` endpoint.
It receives the question, runs the retrieval, calls Ollama, and returns the answer.

**Open WebUI** — the chat interface

A web app that looks like a private ChatGPT. Developers open it in their browser,
type questions, and get answers. It talks to FastAPI behind the scenes.
Access is controlled by your existing login system (SSO/LDAP).

---

## How a question gets answered — step by step

Taking the question: *"Where is the RabbitMQ publisher for payment events?"*

| Step | What happens | Tool used |
|------|-------------|-----------|
| 1 | Question is converted to a vector (list of numbers) | Ollama embed |
| 2 | Qdrant searches for the 5 most similar vectors in the knowledge store | Qdrant |
| 3 | "publisher" keyword detected → Neo4j queried for queue relationships | Neo4j |
| 4 | Retrieved chunks + graph context assembled into a prompt | LangChain |
| 5 | Ollama reads the prompt and writes an answer | Ollama |
| 6 | Answer + source file references returned to developer | FastAPI |

Total time: typically 5–20 seconds depending on server hardware.

---

## How documentation stays current

```
Developer pushes code to GitHub main branch
                ↓
GitHub Actions detects changed .java or .cs files
                ↓
Self-hosted runner (inside our network) runs index_diff.py
                ↓
Only the changed files are re-indexed (fast — usually 1-3 minutes)
                ↓
Updated chunks upserted to Qdrant
Updated relationships updated in Neo4j
Updated service docs written to MinIO
```

The self-hosted runner is critical — it means the indexing process runs inside our network
and no code is sent to GitHub's servers for processing.

---

## Deployment

Everything runs in Docker on a single EC2 instance (or on-prem server).
For the prototype, one `docker compose up -d` starts everything.
Recommended hardware: r6i.2xlarge (8 vCPU, 64 GB RAM) or equivalent on-prem server.

For production scale (all 200+ repos), the same Docker Compose works —
just with a larger instance (p3.2xlarge adds GPU for ~10× faster Ollama inference)
and potentially separate hosts for Qdrant and Neo4j.
