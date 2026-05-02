# GitHub Copilot Instructions

> Copilot reads this file automatically for every chat in this repo.
> These rules apply at all times, for all agents.

---

## What this project is

A private, on-premise codebase intelligence platform for a healthcare company.
It indexes 200+ Java and .NET repositories and lets developers ask plain English questions.
Everything runs on our own servers. Nothing leaves our network.

---

## Rules that always apply — never break these

1. **No external AI APIs** — no OpenAI, no Anthropic, no Azure OpenAI, no HuggingFace inference.
   All AI inference uses Ollama running locally. Host defined in DECISIONS.md.

2. **No hardcoded credentials** — all passwords, tokens, and keys come from environment variables.

3. **Always read DECISIONS.md first** — before writing any code that connects to a service,
   check DECISIONS.md for the exact collection name, port, field name, or model name.
   Do not invent new values — use what is already decided.

4. **No data egress** — no code may send source code, queries, answers, or embeddings
   to any external service or URL.

5. **Error handling is not optional** — every call to Qdrant, Neo4j, Ollama, MinIO, or Sourcegraph
   must have a try/except with a meaningful error message.

6. **Explain your decisions** — when making a non-obvious choice, add a comment explaining why.

7. **Sourcegraph access** — `SOURCEGRAPH_URL` and `SOURCEGRAPH_TOKEN` come from environment variables.
   If Sourcegraph is unreachable, log a warning and continue — never crash.

8. **Indexer folder structure** — `indexer/clients/` contains API clients (e.g. Sourcegraph).
   `indexer/parsers/` contains language parsers (Java, .NET). Do not mix these.

---

## How the .github folder is organised

```
.github/
├── copilot-instructions.md    ← this file — global rules, always active
├── agents/                    ← WHO does the work and HOW they behave
│   ├── orchestrator.agent.md  ← plans tasks, coordinates agents, gates phases
│   ├── planner.agent.md       ← produces detailed specs before any code is written
│   ├── coder.agent.md         ← writes all code and config
│   ├── reviewer.agent.md      ← reviews code against DECISIONS.md
│   ├── security.agent.md      ← checks for data egress, secrets, HIPAA risks
│   └── docs.agent.md          ← updates DECISIONS.md and docs/
├── phases/                    ← WHAT to build in each phase
│   ├── PHASE1-infrastructure.md
│   ├── PHASE2-indexer.md
│   ├── PHASE3-rag.md
│   └── PHASE4-ui-and-pipeline.md
└── workflows/
    └── index-on-push.yml
```

Agent files = role behaviour (stays the same across all phases)
Phase files = build requirements (one per phase, changes each phase)

---

## Copilot Chat — how to reference files

Always combine an agent file + a phase file + DECISIONS.md.
For the full step-by-step sequence see the Quick Reference section in DECISIONS.md.

Minimal example:

```
#coder.agent.md #PHASE1-infrastructure.md #DECISIONS.md
You are the Coder. Build task 1: docker-compose.yml
```

---

## Key values locked in DECISIONS.md — never change without updating that file

| What | Value |
|------|-------|
| Qdrant collection | `codebase-index` |
| Qdrant payload fields | `repo`, `file`, `method`, `lines`, `language`, `summary`, `indexed_at` |
| Neo4j node types | `Service`, `Queue`, `Endpoint` |
| Neo4j relationship types | `CALLS`, `PUBLISHES_TO`, `CONSUMES_FROM`, `EXPOSES` |
| Ollama model | `deepseek-coder:6.7b` |
| Docker network | `codebase-net` |
| Ports | Qdrant=6333, Neo4j=7687, Ollama=11434, WebUI=3000, FastAPI=8000, MinIO=9000, Sourcegraph=7080 |
| Sourcegraph port | `7080` |
| Sourcegraph URL env var | `SOURCEGRAPH_URL` |
| MinIO bucket | `service-docs` |
