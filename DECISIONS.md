# DECISIONS.md — Shared Build Memory

> This file is the single source of truth across all four build phases.
> Every agent reads this before starting. Every Docs agent appends to it when done.
> Never delete entries — only append. If something changes, add a REVISED entry with the date.

---

## Quick reference — Copilot Chat sequence

Use this every time you start a phase. Replace the phase file with the current one.

Phase files live in `.github/phases/`:
- Phase 1 → `#PHASE1-infrastructure.md`
- Phase 2 → `#PHASE2-indexer.md`
- Phase 3 → `#PHASE3-rag.md`
- Phase 4 → `#PHASE4-ui-and-pipeline.md`

```
# 1. Plan (example for Phase 1 — swap phase file for other phases)
#orchestrator.agent.md #PHASE1-infrastructure.md #DECISIONS.md
You are the Orchestrator. Plan Phase 1. List tasks in order. No code yet.

# 2. Plan one task in detail
#planner.agent.md #PHASE1-infrastructure.md #DECISIONS.md
You are the Planner. Produce a detailed spec for task 1: [exact task name from Orchestrator plan]

# 3. Build the task
#coder.agent.md #PHASE1-infrastructure.md #DECISIONS.md
You are the Coder. Build task 1 exactly as specified in the Planner's spec above.

# 4. Review each file produced
#reviewer.agent.md #DECISIONS.md
You are the Reviewer. Review [filename] just produced.

# 5. Security check each file
#security.agent.md #DECISIONS.md
You are the Security agent. Check [filename] for issues.

# 6. Close the phase (after all tasks reviewed and cleared)
#docs.agent.md #DECISIONS.md
You are the Docs agent. Close Phase 1 — update DECISIONS.md.

# 7. Open next phase
#orchestrator.agent.md #PHASE2-indexer.md #DECISIONS.md
Phase 1 is closed. Plan Phase 2.
```

---

## Locked-in values — never change these without updating this file

| What | Value |
|------|-------|
| Qdrant collection | `codebase-index` |
| Qdrant port | `6333` |
| Neo4j bolt port | `7687` |
| Neo4j browser port | `7474` |
| Ollama port | `11434` |
| Ollama model | `deepseek-coder:6.7b` |
| Open WebUI port | `3000` |
| MinIO API port | `9000` |
| FastAPI port | `8000` |
| Docker network | `codebase-net` |
| FastAPI /ask request | `{ "question": "string" }` |
| FastAPI /ask response | `{ "answer": "string", "sources": [], "graph_context_used": bool }` |
| Neo4j node types | `Service`, `Queue`, `Endpoint`, `Database` |
| Neo4j relationship types | `CALLS`, `PUBLISHES_TO`, `CONSUMES_FROM`, `EXPOSES`, `READS_FROM`, `WRITES_TO` |
| Database node detected tech | RabbitMQ (Queue), SQL / RDS / PostgreSQL (Database) |
| Qdrant payload fields | `repo`, `file`, `method`, `lines`, `language`, `summary`, `indexed_at` |
| Sourcegraph port | `7080` |
| Sourcegraph URL env var | `SOURCEGRAPH_URL` (`http://localhost:7080`) |
| Sourcegraph token env var | `SOURCEGRAPH_TOKEN` |
| MinIO docs bucket | `service-docs` |
| Mermaid diagram type | `graph LR` |
| Generated docs output path | `docs/services/` |
| Architecture overview file | `docs/architecture-overview.md` |
| Indexer clients folder | `indexer/clients/` |
| Sourcegraph client location | `indexer/clients/sourcegraph_client.py` |

---

## Phase 1 — Infrastructure decisions

Date completed:      2026-03-29
Built by:            Coder agent (Phase 1) + Orchestrator review

Target server:
  - Local macOS (developer machine) for prototype
  - Recommended production: r6i.2xlarge (8 vCPU, 64 GB RAM) or equivalent on-prem
  - GPU upgrade path: p3.2xlarge for ~10× faster Ollama inference (Phase 2+)

Prerequisites confirmed:
  [x] Docker and Docker Compose installed
  [ ] Self-hosted GitHub Actions runner registered to the organisation
  [ ] GitHub fine-grained personal access token (read-only, contents:read)
  [ ] 5–8 target repos identified (payment, order, notification, messaging)
  [ ] Internal DNS entry or IP for accessing the UI from developer workstations

Service ports confirmed:
  - Qdrant:      6333 (HTTP), 6334 (gRPC)
  - Neo4j:       7474 (browser), 7687 (bolt)
  - Ollama:      11434
  - Open WebUI:  3000
  - MinIO:       9000 (API), 9001 (console)
  - FastAPI:     8000 (Phase 3)

Volume paths:
  - Qdrant data:    ./qdrant_data
  - Neo4j data:     ./neo4j_data
  - Ollama models:  ./ollama_models
  - MinIO data:     ./minio_data
  - Open WebUI:     ./openwebui_data

Model pulled:        deepseek-coder:6.7b (pull command: docker exec ollama ollama pull deepseek-coder:6.7b)
Model SHA-256 verified: [ ] yes / [ ] no
Model verified working:  [ ] yes / [ ] no
GPU enabled:         [ ] no (CPU-only; GPU block commented out in docker-compose.yml)

Environment variables used: see infrastructure/.env.example

Changes from original plan and why:
  1. Ollama healthcheck: changed from ["CMD", "curl", "-f", "http://localhost:11434"]
     to ["CMD", "ollama", "list"] — curl is not present in the ollama/ollama:latest
     image. Also added start_period: 60s and retries: 5 to allow for initialisation time.
  2. All other scaffold files (docker-compose.yml, .env.example, verify_services.sh)
     were already spec-compliant; only minor comments were added.

---

## Phase 2 — Indexer decisions

Date completed:      2026-04-04
Built by:            Coder agent (Phase 2); Reviewer + Security agent approved all tasks

Files built (in order):
  Task 1  indexer/requirements.txt
  Task 2  indexer/parsers/__init__.py, java_parser.py, dotnet_parser.py
  Task 3  indexer/clients/__init__.py, sourcegraph_client.py
  Task 4  indexer/writers/__init__.py, qdrant_writer.py, neo4j_writer.py
  Deferred to Phase 3: indexer/index_repos.py, indexer/index_diff.py
    (pipeline entry points deferred so Phase 3 RAG work can be parallelised;
     the writers API is complete and ready to be called)

Qdrant collection name:   codebase-index
Qdrant distance metric:   Cosine
Qdrant vector dimensions: probed at runtime via _get_embedding_dimension() in
                          qdrant_writer.py — never hardcoded. The method fires
                          one test embedding call to Ollama at startup and uses
                          len(response["embedding"]) as the collection size.
                          deepseek-coder:6.7b returns 4096-dimensional vectors.

Embedding model:          deepseek-coder:6.7b  (same model used for summarisation)
Embedding input:          "{method_name} {ollama_summary}" — combining the name
                          with the summary improves recall for method-name queries
                          while retaining semantic richness from the summary.

Chunk strategy:
  - Java:   method-level AST extraction via tree-sitter-java 0.21.x
            also extracts class_declaration and interface_declaration as chunks
  - .NET:   method-level AST extraction via tree-sitter-c-sharp 0.21.x
            also extracts class_declaration and interface_declaration as chunks
  - Chunk size: AST-node-bounded (not fixed-token); raw_code truncated to
                2000 chars (_MAX_CODE_CHARS) before sending to Ollama for summary.
  - CHANGE from original plan: original spec said 512-token / 50-token-overlap
    fixed chunks. Changed to AST-node-bounded chunks because method boundaries
    are more meaningful than arbitrary token windows for code search.

Qdrant payload fields per chunk (exactly 7 — locked in DECISIONS.md):
  - repo:        (string) repository name passed to QdrantWriter constructor
  - file:        (string) repo-relative file path from chunk["file"]
  - method:      (string) method or class name from chunk["name"]
  - lines:       (string) e.g. "12-45" from chunk["lines"]
  - language:    (string) "java" or "csharp" passed to QdrantWriter constructor
  - summary:     (string) Ollama-generated plain English summary
  - indexed_at:  (ISO 8601 UTC timestamp, generated at write time)

Point ID strategy: uuid5(NAMESPACE_URL, "{repo}:{file}:{name}:{lines}")
  — deterministic; re-indexing the same method overwrites the existing point.

Neo4j nodes created (exact schema):
  - Service   { name: string, repo: string }
  - Queue     { name: string }             — name derived as "{service}-queue"
  - Endpoint  { path: string, method: string }  — method is GET/POST/PUT/DELETE
  - Database  { name: string, type: string }    — type: "sql"|"postgresql"|"rds"

Neo4j relationships (all MERGE, never CREATE):
  - (Service)-[:CALLS]->(Service)
  - (Service)-[:PUBLISHES_TO]->(Queue)
  - (Service)-[:CONSUMES_FROM]->(Queue)
  - (Service)-[:EXPOSES]->(Endpoint)
  - (Service)-[:READS_FROM]->(Database)
  - (Service)-[:WRITES_TO]->(Database)

READS_FROM vs WRITES_TO heuristic (from chunk["name"]):
  WRITES_TO:  method name matches \b(save|update|insert|delete|persist|create|put)\b
  READS_FROM: method name matches \b(find|get|query|select|load|fetch|list)\b
  Ambiguous:  create both READS_FROM and WRITES_TO

Queue direction heuristic (from chunk["name"]):
  PUBLISHES_TO:   method name matches \b(publish|send|producer|emit|dispatch)\b
  CONSUMES_FROM:  method name matches \b(listen|consume|receive|handler|subscriber|process)\b
  Ambiguous:      create both

HTTP direction heuristic (from chunk["raw_code"]):
  EXPOSES Endpoint:  raw_code contains any of @GetMapping, @PostMapping, @PutMapping,
                     @DeleteMapping, @RequestMapping, [HttpGet], [HttpPost], [HttpPut],
                     [HttpDelete], [Route], [ApiController]
  CALLS Service:     raw_code contains any of RestTemplate, WebClient, FeignClient,
                     HttpClient, IHttpClientFactory
                     (target is "unknown-service" placeholder; Sourcegraph Step 5
                     provides more accurate CALLS edges for cross-repo calls)

Pattern detection — Java (.java files):
  Import-level (file-scope):
    RabbitMQ:    org.springframework.amqp, com.rabbitmq
    HTTP:        org.springframework.web, feign., javax.ws.rs
    SQL/DB:      javax.persistence, org.springframework.data, java.sql,
                 org.springframework.jdbc
    PostgreSQL:  org.postgresql  → db_type = "postgresql"
  Body-level (per AST node):
    RabbitMQ:    rabbitTemplate, @RabbitListener, channel.basicPublish,
                 convertAndSend
    HTTP:        RestTemplate, WebClient, FeignClient, @GetMapping,
                 @PostMapping, @RequestMapping
    SQL/DB:      @Repository, JdbcTemplate, EntityManager, DataSource,
                 @Query, JpaRepository

Pattern detection — .NET (.cs files):
  Using-level (file-scope):
    RabbitMQ:    RabbitMQ.Client
    HTTP:        System.Net.Http, Microsoft.AspNetCore
    SQL/DB:      System.Data, Microsoft.EntityFrameworkCore
    PostgreSQL:  Npgsql  → db_type = "postgresql"
  Body-level (per AST node):
    RabbitMQ:    IModel, BasicPublish, EventingBasicConsumer, IRabbitMQService
    HTTP:        HttpClient, IHttpClientFactory, [HttpGet], [HttpPost],
                 [Route], [ApiController]
    SQL/DB:      DbContext, IRepository, SqlConnection, SqlCommand,
                 NpgsqlConnection, IDbConnection

Sourcegraph integration:
  Location:         indexer/clients/sourcegraph_client.py
  SOURCEGRAPH_URL:  from env SOURCEGRAPH_URL  (default: http://localhost:7080)
  SOURCEGRAPH_TOKEN: from env SOURCEGRAPH_TOKEN
  Graceful fallback: is_available() returns False when unreachable;
                     search_symbol() and search_callers() return [] on any error;
                     indexing run always completes even if Sourcegraph is down.

Environment variables introduced in Phase 2:
  QDRANT_HOST      Qdrant hostname              (default: localhost)
  QDRANT_PORT      Qdrant HTTP port             (default: 6333)
  QDRANT_API_KEY   Optional Qdrant API key      (default: empty — not required on LAN)
  OLLAMA_HOST      Ollama base URL              (default: http://localhost:11434)
  OLLAMA_MODEL     Ollama model name            (default: deepseek-coder:6.7b)
  NEO4J_URI        Neo4j bolt URI               (default: bolt://localhost:7687)
  NEO4J_USER       Neo4j username               (default: neo4j)
  NEO4J_PASSWORD   Neo4j password               (required; writer disabled if unset)
  SOURCEGRAPH_URL  Sourcegraph base URL         (default: http://localhost:7080)
  SOURCEGRAPH_TOKEN Sourcegraph personal token  (optional)

Test repo indexed:    not run yet (index_repos.py deferred to Phase 3)
Index run time:       not measured yet
Total chunks created: not measured yet

Known non-blocking issue (Reviewer finding — fix before Phase 3):
  indexer/writers/qdrant_writer.py line 29: `import re` is present but unused.
  Remove before Phase 3 code review.

Changes from original plan and why:
  1. Chunk strategy changed from fixed 512-token/50-overlap windows to
     AST-node-bounded method chunks. Reason: method boundaries are more
     semantically meaningful for code search than arbitrary token windows.
     The _MAX_CODE_CHARS=2000 limit still prevents oversized Ollama prompts.
  2. index_repos.py and index_diff.py (pipeline entry points) deferred to
     Phase 3. Reason: all writer and parser APIs are complete; the entry
     points have no blockers other than prioritisation. Phase 3 RAG work
     can begin in parallel with these finishing.
  3. Neo4j Service node schema is { name, repo } not { name, repo, language }.
     language removed from Service node — language is stored in Qdrant payload
     alongside the chunk, not on the graph node. Keeping the graph schema
     minimal avoids updating Service nodes when the same service is indexed
     in multiple languages.

---

## Phase 3 — RAG chain decisions

Date completed:      2026-04-04
Built by:            Coder agent (Phase 3); Reviewer + Security agent approved all tasks

Files built (in order):
  Task 1  rag/requirements.txt
  Task 2  rag/chain.py
  Task 3  rag/app.py, rag/__init__.py
  Task 4  rag/test_queries.py
  Task 5  indexer/index_repos.py, indexer/index_diff.py
          + indexer/writers/qdrant_writer.py (removed unused `import re`)

FastAPI endpoints (all in rag/app.py, port 8000):
  POST /ask                   — primary question-answering endpoint
  GET  /health                — probes Qdrant, Neo4j, Ollama independently;
                                returns "ok" / "fail" / "disabled" per service
  GET  /stats                 — Qdrant vector count + Neo4j node counts by label
  POST /v1/chat/completions   — Open WebUI compatibility wrapper (calls /ask internally)

FastAPI request/response (locked in table above):
  Request:   { "question": "string" }
  Response:  { "answer": "string", "sources": [...], "graph_context_used": bool }
  Sources array fields per item: repo, file, method, lines, score

Qdrant collection: codebase-index  (same as Phase 2 — no change)
Qdrant top-k:      5  (constant _TOP_K in rag/chain.py)

Neo4j trigger condition:
  _query_neo4j() is called when the question contains at least one word (after
  stripping punctuation and lowercasing) that exactly matches a member of
  _NEO4J_KEYWORDS.  Exact match, not substring.

Neo4j trigger keywords (exactly 9 — frozenset in rag/chain.py line 89):
  calls, depends, breaks, publisher, consumer, queue, downstream, affect, impact

Ollama model:  deepseek-coder:6.7b  (read from OLLAMA_MODEL env var)
Ollama host:   read from OLLAMA_HOST env var
               default for local dev:  http://localhost:11434
               set in Docker env:      http://ollama:11434

Prompt template (verbatim — in rag/chain.py constant _PROMPT_TEMPLATE):
  You are a senior software architect helping developers understand a large codebase.
  You have access to the following code context retrieved from the codebase:

  {qdrant_context}

  {neo4j_context}

  Answer the following question clearly and concisely.
  Always mention the exact repository name and file path when referring to code.
  If you are unsure, say so — do not guess.

  Question: {question}

Test queries (exact text, from rag/test_queries.py):
  [ ] "Where is the RabbitMQ publisher for payment events?"
  [ ] "How does the payment flow work?"
  [ ] "What API endpoints does the order service expose?"
  [ ] "If I change PaymentService, what other services might be affected?"

  PASS criterion per query: answer field non-empty AND len(sources) >= 1
  Queries have not been run against live services yet — boxes above will be
  ticked when a live index run has been completed in Phase 4.

Average response time:  not measured yet (requires live Ollama + indexed data)

New environment variable introduced in Phase 3:
  OLLAMA_TIMEOUT   Soft warning threshold in seconds for Ollama generate calls.
                   If the call exceeds this value a WARNING is logged but the
                   call is not killed.  (default: 120)  Used in rag/chain.py only.

Indexer entry points (completed in Task 5, deferred from Phase 2):
  indexer/index_repos.py — full-repo indexing CLI
    --repo PATH (required)   absolute or relative path to repo root
    --name NAME (required)   short canonical repo name
    --language auto|java|csharp (default: auto — detects from file counts)
    Five-step flow: collect → parse → Qdrant write → Neo4j write → Sourcegraph

  indexer/index_diff.py — incremental indexing CLI
    --name NAME (required)   short canonical repo name
    --repo PATH (default: .)  repo root
    --files FILE [FILE …]    explicit list of changed files  }  mutually
    --git-diff GIT_REF        e.g. HEAD~1 or a commit SHA   }  exclusive
    Deletes old Qdrant points via FilterSelector(FieldCondition("file", ...))
    before re-inserting, so stale vectors are always removed on re-index.

Changes from original plan and why:
  1. Ollama host default is http://localhost:11434, not http://ollama:11434.
     The value is read from OLLAMA_HOST env var at runtime; docker-compose
     will set OLLAMA_HOST=http://ollama:11434 for the FastAPI container in
     Phase 4.  Hardcoding the Docker service name in the default would break
     local development outside Docker.
  2. Neo4j is non-fatal in chain.py — if NEO4J_PASSWORD is unset or Neo4j
     is unreachable, graph_context_used returns False and the answer is built
     from Qdrant context only.  This preserves usability during development
     when Neo4j may not be running.
  3. Open WebUI compatibility endpoint (POST /v1/chat/completions) added to
     app.py.  Not in the original spec.  Added because Open WebUI expects an
     OpenAI-compatible endpoint; wrapping /ask avoids duplicating any logic.
  4. index_repos.py and index_diff.py were planned for Phase 2 but deferred
     and completed in Phase 3 Task 5.  No functional change from the spec.

---

## Phase 4 — Pipeline and UI decisions

Date completed:      2026-04-04
Built by:            Coder agent (Phase 4); Reviewer + Security agent approved all tasks

Files built (in order):
  Task 1  .github/workflows/index-on-push.yml
  Task 2  indexer/generate_docs.py
  Task 3  scripts/demo_queries.sh  (pre-existing scaffold validated — not recreated)
  Task 4  docs/05-how-to-run.md   (Steps 1–10 complete, including generate_docs + demo)

Also patched (not originally planned — arose during Task 4 review):
  9 Python files given explicit dotenv_path — see "Changes from original plan" below.
  Files patched: indexer/index_repos.py, indexer/index_diff.py, indexer/generate_docs.py,
  indexer/clients/sourcegraph_client.py, indexer/writers/qdrant_writer.py,
  indexer/writers/neo4j_writer.py, rag/chain.py, rag/app.py, rag/test_queries.py

GitHub Actions workflow (.github/workflows/index-on-push.yml):
  Runner type:      self-hosted  (not ubuntu-latest — code must not leave our network)
  Runner scope:     register at org level or repo level depending on your GitHub plan
  Trigger branch:   main
  Trigger paths:    **.java  **.cs
  Skip logic:       if git diff HEAD~1 HEAD returns no .java/.cs files, all steps are skipped
  Indexer command:  python indexer/index_diff.py --files <space-separated file list>
  OLLAMA_MODEL:     deepseek-coder:6.7b  (hardcoded as env var in workflow — not sensitive)

  Required GitHub secrets (set in org or repo settings before first run):
    QDRANT_HOST       IP or hostname of the Qdrant server
    QDRANT_PORT       6333
    NEO4J_URI         bolt://YOUR_SERVER_IP:7687
    NEO4J_USER        neo4j
    NEO4J_PASSWORD    your Neo4j password
    OLLAMA_HOST       http://YOUR_SERVER_IP:11434

generate_docs.py (indexer/generate_docs.py):
  CLI:  python indexer/generate_docs.py
        python indexer/generate_docs.py --output-dir docs/services/ --overview-file docs/architecture-overview.md
  Source data:  Neo4j (Service nodes + CALLS/PUBLISHES_TO/CONSUMES_FROM/EXPOSES relationships)
                + Qdrant (class-level summary chunks for each service)
  Output:
    Local disk:   --output-dir (default: docs/services/)
    MinIO:        always uploaded to bucket service-docs (port 9000)
    Confluence:   skipped unless CONFLUENCE_URL + CONFLUENCE_TOKEN + CONFLUENCE_SPACE_KEY are all set
    Repo commit:  skipped unless COMMIT_DOCS_TO_REPO=true

  New environment variables introduced in Phase 4 (add to infrastructure/.env for local dev):
    MINIO_ENDPOINT        localhost:9000     (host:port — no scheme)
    MINIO_ACCESS_KEY      minioadmin
    MINIO_SECRET_KEY      (set to a strong password)
    MINIO_SECURE          false              (true if MinIO is behind TLS)
    CONFLUENCE_URL        (optional)
    CONFLUENCE_TOKEN      (optional — Confluence personal access token)
    CONFLUENCE_SPACE_KEY  (optional — Confluence space key, e.g. DOCS)
    COMMIT_DOCS_TO_REPO   false              (set to "true" to git add/commit/push docs)

Open WebUI configuration (manual — no code):
  After docker compose is up:
    1. Open http://localhost:3000 → Settings → Connections
    2. Add OpenAI-compatible connection: URL = http://YOUR_SERVER_IP:8000, API Key = any string
    3. Settings → Models → rename the /ask endpoint to "Codebase Q&A"
    4. Set as default model
  The /v1/chat/completions wrapper in rag/app.py (built in Phase 3) handles this integration.

Demo scenarios (not yet run against live services — require a live indexed dataset):
  [ ] Demo 1 — "How does the payment flow work end to end?"
  [ ] Demo 2 — "Where is the RabbitMQ publisher for payment events and who consumes it?"
  [ ] Demo 3 — push a .java/.cs file → verify docs update in < 5 minutes
Leadership demo date:   not yet scheduled

Post-prototype roadmap items noted:
  - Expand to all 200+ repos via org-level GitHub Actions workflow templates
  - Add GPU (p3.2xlarge) for ~10× faster Ollama inference
  - Role-based access control — restrict repo answers by developer group
  - Sourcegraph OSS for deep cross-repo symbol search (complements vector search)
  - Service dependency dashboard in Open WebUI home page using Neo4j graph data
  - Automate Confluence page updates (current implementation is create-only)

Changes from original plan and why:
  1. load_dotenv() explicit path — not in original Phase 4 spec.  Bare load_dotenv()
     searches from CWD; the .env file lives at infrastructure/.env, not the repo root.
     When any script is run from the repo root (as documented in Steps 4, 5, 9), bare
     load_dotenv() silently finds nothing and all env vars remain unset.  Fixed by
     changing all 9 Python files to use:
       load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().[parent…] / "infrastructure" / ".env")
     Path depth: 1-level files (indexer/, rag/) use .parent.parent;
                 2-level files (indexer/clients/, indexer/writers/) use .parent.parent.parent.
     This makes the path file-relative and CWD-independent.  In Docker, load_dotenv()
     is a no-op because env vars are already injected by docker-compose.

  2. rag/app.py import ordering — from rag.chain import ask was originally placed before
     load_dotenv().  chain.py self-loads env vars, so this worked silently, but the
     correct pattern is to load env first and then import downstream modules.  Reordered
     so load_dotenv() fires on line 52 and the chain import follows on line 57.
     Added # noqa: E402 to the deferred import line.

  3. docs/05-how-to-run.md Step count — original Phase 3 runbook had 8 steps.
     Phase 4 added Steps 9 (generate_docs.py) and 10 (leadership demo checklist).
     All infrastructure/.env references are now fully qualified — zero bare .env
     references remain in the runbook.
