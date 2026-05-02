# Codebase Intelligence Platform

> A fully private, on-premise AI assistant that lets developers ask questions about 200+ repositories in plain English.
> No data leaves your network. Built for healthcare.

---

## What this does

| You ask | It answers |
|---|---|
| "How does payment flow work?" | Plain English walkthrough with file references |
| "Where is the RabbitMQ publisher for payment events?" | Exact repo, file, and line number |
| "If I change PaymentService, what breaks?" | List of all downstream services |
| "What API endpoints does OrderService expose?" | Auto-generated API inventory |

---

## Tech stack (all open-source, all on-premise)

| Component | Tool | Purpose |
|---|---|---|
| Code search | Sourcegraph OSS | Search across all 200+ repos |
| Code parsing | tree-sitter | Understand Java + .NET structure |
| Local AI | Ollama + DeepSeek Coder | Answer questions — never leaves network |
| Semantic search | Qdrant | Find code by meaning, not keywords |
| Dependency graph | Neo4j | Map service-to-service relationships |
| Doc storage | MinIO / S3 | Store generated docs and diagrams |
| RAG orchestration | LangChain | Wire everything together |
| Chat UI | Open WebUI | Developer-facing chat interface |
| Diagrams | Mermaid.js | Auto-generated architecture diagrams |
| CI/CD updates | GitHub Actions | Re-index on every code push |

---

## Repository structure

```
codebase-intelligence/
│
├── README.md                              ← you are here
├── DECISIONS.md                           ← shared memory — fill this as you build
│
├── docs/                                  ← plain English documentation
│   ├── 01-what-we-are-building.md
│   ├── 02-architecture.md
│   ├── 03-component-guide.md
│   ├── 04-security-and-healthcare.md
│   └── 05-how-to-run.md
│
├── infrastructure/                        ← Docker Compose and config (Phase 1 output)
│   ├── docker-compose.yml
│   └── .env.example
│
├── indexer/                               ← Indexing pipeline (Phase 2 output)
│   ├── index_repos.py
│   ├── index_diff.py
│   ├── parsers/
│   │   ├── java_parser.py
│   │   └── dotnet_parser.py
│   ├── clients/
│   │   └── sourcegraph_client.py
│   ├── writers/
│   │   ├── qdrant_writer.py
│   │   └── neo4j_writer.py
│   └── requirements.txt
│
├── rag/                                   ← RAG chain and API (Phase 3 output)
│   ├── app.py
│   ├── chain.py
│   ├── test_queries.py
│   └── requirements.txt
│
├── scripts/                               ← Utility scripts
│   ├── verify_services.sh                 ← health check all Docker services
│   └── demo_queries.sh                    ← run the 3 leadership demo scenarios
│
└── .github/
    ├── copilot-instructions.md            ← global Copilot rules (auto-loaded)
    │
    ├── agents/                            ← WHO does the work and HOW they behave
    │   ├── orchestrator.agent.md         ← coordinates all agents, gates phases
    │   ├── coder.agent.md                ← writes code (rules + standards)
    │   ├── reviewer.agent.md             ← reviews against DECISIONS.md
    │   ├── security.agent.md             ← healthcare/HIPAA/data egress checks
    │   └── docs.agent.md                 ← updates DECISIONS.md and docs/
    │
    ├── phases/                            ← WHAT to build in each phase
    │   ├── PHASE1-infrastructure.md      ← docker-compose, env, verify script
    │   ├── PHASE2-indexer.md             ← tree-sitter parser, Qdrant + Neo4j writers
    │   ├── PHASE3-rag.md                 ← LangChain chain, FastAPI /ask endpoint
    │   └── PHASE4-ui-and-pipeline.md     ← Open WebUI, GitHub Actions, demo script
    │
    └── workflows/
        └── index-on-push.yml             ← auto-index on merge (Phase 4 output)
```

---

## How to build — agent orchestration in Copilot Chat

**Two types of files work together:**

| Type | Folder | Purpose | Example |
|---|---|---|---|
| Agent files | `.github/agents/` | Define WHO does work and HOW they behave | `#CODER.md` |
| Phase files | `.github/phases/` | Define WHAT to build in each phase | `#PHASE1-infrastructure.md` |

You always combine one agent file + one phase file + DECISIONS.md in each message.

**The sequence for every phase:**

```
# Step 1 — Orchestrator plans (no code yet)
#ORCHESTRATOR.md #PHASE1-infrastructure.md #DECISIONS.md
You are the Orchestrator. Plan Phase 1. List tasks in order. No code yet.

# Step 2 — Coder builds one task at a time
#CODER.md #PHASE1-infrastructure.md #DECISIONS.md
You are the Coder. Build task 1: docker-compose.yml

# Step 3 — Reviewer checks each file
#REVIEWER.md #DECISIONS.md
You are the Reviewer. Review docker-compose.yml just produced.

# Step 4 — Security checks each file
#SECURITY.md #DECISIONS.md
You are the Security agent. Check docker-compose.yml for issues.

# Step 5 — Docs closes the phase
#DOCS.md #DECISIONS.md
You are the Docs agent. Close Phase 1 — update DECISIONS.md.

# Step 6 — move to next phase
#ORCHESTRATOR.md #PHASE2-indexer.md #DECISIONS.md
Phase 1 is closed. Plan Phase 2.
```

**The golden rule:** Do not move to the next phase until you can explain what was built and why.
DECISIONS.md is your checkpoint — if it is not filled in, the phase is not done.

---

## Phase summary

| Phase | Folder | Days | Goal |
|---|---|---|---|
| 1 | `infrastructure/` | 1–2 | All services running in Docker |
| 2 | `indexer/` | 3–5 | Code indexed into Qdrant + Neo4j |
| 3 | `rag/` | 6–7 | Q&A endpoint answering real questions |
| 4 | `.github/workflows/` + UI | 8–14 | Live demo ready for leadership |

---

## Quick start (after Phase 1 is complete)

```bash
# 1. Clone this repo
git clone https://github.com/YOUR_ORG/codebase-intelligence
cd codebase-intelligence

# 2. Copy env file and fill in values
cp infrastructure/.env.example infrastructure/.env

# 3. Start all services
docker compose -f infrastructure/docker-compose.yml up -d

# 4. Pull the AI model (one-time — then air-gapped)
docker exec ollama ollama pull deepseek-coder:6.7b

# 5. Verify everything is running
bash scripts/verify_services.sh

# 6. Open the chat UI
# http://YOUR_SERVER_IP:3000

# 7. Open Sourcegraph (code search across all repos)
# http://YOUR_SERVER_IP:7080
```

---

## Security

- Zero data egress — all AI inference runs on-premise via Ollama
- No patient data is indexed — source code only (.java and .cs files)
- GitHub Actions uses a read-only token scoped to code files only
- All services run inside private network / AWS VPC
- Self-hosted GitHub Actions runner — code never processed on GitHub servers
- Full audit log via Open WebUI and GitHub Actions history

See `docs/04-security-and-healthcare.md` for full details.
