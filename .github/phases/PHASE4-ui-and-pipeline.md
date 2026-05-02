# Phase 4 — UI, Pipeline, and Demo
## How to use this file
Attach this file with `#PHASE4-ui-and-pipeline.md` in Copilot Chat when using the **Coder** agent.
The Coder agent reads this file to know exactly what to build in this phase.

---

## CONTEXT — read this first

We are in the final phase. All core services are built and working.

**Read DECISIONS.md — all four phase sections — before writing anything.**

What is already working:
- Phase 1: All services running in Docker (Qdrant, Neo4j, Ollama, Open WebUI, MinIO)
- Phase 2: Indexer populates Qdrant and Neo4j from Java and .NET repos
- Phase 3: FastAPI /ask endpoint returns answers with sources

What we need to finish:
1. Connect Open WebUI to our FastAPI /ask endpoint
2. Build the GitHub Actions workflow for auto-indexing on push
3. Build a doc generator that creates Markdown service summaries
4. Prepare the demo for leadership

---

## YOUR GOAL FOR THIS PHASE

Produce these files:
- `.github/workflows/index-on-push.yml`
- `indexer/generate_docs.py`
- `scripts/demo_queries.sh`
- `docs/05-how-to-run.md` (already exists — review and update the "How to run" section with Phase 4 specifics)

And configure Open WebUI (instructions, not code — it is configured via UI).

---

## DETAILED REQUIREMENTS

### index-on-push.yml — GitHub Actions workflow

Build the complete working YAML below. Do not use placeholder comments for steps — write real working steps.

```yaml
name: Index changed code

on:
  push:
    branches: [main]
    paths:
      - '**.java'
      - '**.cs'

jobs:
  index:
    runs-on: self-hosted

    steps:
      - name: Checkout repo
        uses: actions/checkout@v4
        with:
          fetch-depth: 2

      - name: Get changed files
        id: changed
        run: |
          FILES=$(git diff --name-only HEAD~1 HEAD -- '*.java' '*.cs' | tr '\n' ' ')
          echo "files=$FILES" >> $GITHUB_OUTPUT
          if [ -z "$FILES" ]; then
            echo "No .java or .cs files changed — skipping index"
            echo "skip=true" >> $GITHUB_OUTPUT
          else
            echo "skip=false" >> $GITHUB_OUTPUT
          fi

      - name: Set up Python
        if: steps.changed.outputs.skip != 'true'
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        if: steps.changed.outputs.skip != 'true'
        run: pip install -r indexer/requirements.txt

      - name: Run incremental indexer
        if: steps.changed.outputs.skip != 'true'
        env:
          QDRANT_HOST: ${{ secrets.QDRANT_HOST }}
          QDRANT_PORT: ${{ secrets.QDRANT_PORT }}
          NEO4J_URI: ${{ secrets.NEO4J_URI }}
          NEO4J_USER: ${{ secrets.NEO4J_USER }}
          NEO4J_PASSWORD: ${{ secrets.NEO4J_PASSWORD }}
          OLLAMA_HOST: ${{ secrets.OLLAMA_HOST }}
          OLLAMA_MODEL: deepseek-coder:6.7b
        run: |
          python indexer/index_diff.py --files ${{ steps.changed.outputs.files }}

      - name: Write step summary
        if: steps.changed.outputs.skip != 'true' && success()
        run: |
          echo "## Indexing complete" >> $GITHUB_STEP_SUMMARY
          echo "Files indexed: ${{ steps.changed.outputs.files }}" >> $GITHUB_STEP_SUMMARY
```

Required GitHub org secrets (set these before the workflow runs):
- QDRANT_HOST — your server IP
- QDRANT_PORT — 6333
- NEO4J_URI — bolt://YOUR_SERVER_IP:7687
- NEO4J_USER — neo4j
- NEO4J_PASSWORD — your Neo4j password
- OLLAMA_HOST — http://YOUR_SERVER_IP:11434

### generate_docs.py — documentation generator

This script reads from Qdrant and Neo4j and generates a Markdown file for each service.

For each Service node in Neo4j, generate a file at `docs/services/{service-name}.md` with:

```markdown
# {Service Name}

> Auto-generated on {date}. Do not edit manually — regenerated on every push.

## What this service does
{2-3 sentence summary from Qdrant — find the class-level summary chunk}

## Repository
{repo name and link}

## API endpoints
| Method | Path | Description |
|--------|------|-------------|
{from Neo4j Endpoint nodes linked to this service}

## Message queues
**Publishes to:**
{list of queues this service publishes to}

**Consumes from:**
{list of queues this service consumes from}

## Calls these services
{list of services this service makes HTTP calls to}

## Called by these services
{list of services that call this service}
```

Data for 'Calls these services' and 'Called by these services' comes from
Neo4j CALLS relationships which include:
- Direct HTTP calls detected by tree-sitter within files
- Cross-repo calls discovered by Sourcegraph and written by sourcegraph_client.py

This means the generated docs show the COMPLETE call graph across all 200 repos,
not just within a single repository.

Run with: `python indexer/generate_docs.py --output-dir docs/services/`

**Where generated docs are stored:**
- Always write to MinIO (MINIO_ENDPOINT from environment)
- Optional: also push to Confluence using its REST API if CONFLUENCE_URL and CONFLUENCE_TOKEN are set in .env
- Optional: commit directly to the target repository if COMMIT_DOCS_TO_REPO=true in .env

Both optional destinations are off by default — activated by environment variables, never hardcoded.

### demo_queries.sh — leadership demo script

A scaffold already exists at `scripts/demo_queries.sh` with all three complete demo scenarios.
When working on Phase 4, review and validate that file — do not recreate it.

The three scenarios it covers:
1. "How does the payment flow work end to end?" — tests full RAG + sources
2. "Where is the RabbitMQ publisher for payment events and who consumes it?" — tests graph context
3. "If I change PaymentService, what other services might be affected?" — tests impact analysis

Make sure the ENDPOINT variable in the script uses the correct server IP from DECISIONS.md.

---

## OPEN WEBUI CONFIGURATION (manual steps, not code)

After Open WebUI is running at http://localhost:3000:

1. Log in as admin
2. Go to Settings → Connections
3. Add a new OpenAI-compatible connection:
   - Name: Codebase Intelligence
   - URL: http://YOUR_SERVER_IP:8000
   - API Key: (leave blank or put any string)
4. Go to Settings → Models
5. The /ask endpoint will appear as a model — rename it "Codebase Q&A"
6. Set it as the default model
7. Test with: "Where is the RabbitMQ publisher for payment events?"

Note: Open WebUI expects an OpenAI-compatible API. Update app.py to add an
`/v1/chat/completions` endpoint that wraps /ask in the OpenAI response format.
Add this endpoint to rag/app.py.

---

## WHAT TO WRITE IN DECISIONS.md WHEN DONE

Append to the Phase 4 section:
- GitHub Actions runner type (org-level or repo-level)
- Open WebUI endpoint URL configured
- Which demo scenarios work end to end
- Whether Confluence or repo-commit doc publishing is configured
- Demo date
- Post-prototype roadmap items to carry forward

---

## POST-PROTOTYPE ROADMAP (record in DECISIONS.md, do not build in this phase)

If the prototype is approved by leadership, the following items expand it to production scale.
Document these in DECISIONS.md Phase 4 section so the next phase has the full picture:

- Expand indexing to all 200+ repos using org-level GitHub Actions workflow templates (one config, no per-repo setup)
- Add GPU (p3.2xlarge instance) for ~10× faster Ollama inference
- Role-based access control — restrict which developer groups can see which repo answers
- Add Sourcegraph OSS for deep cross-repo symbol search (complements vector search)
- Build service dependency dashboard in Open WebUI home page using Neo4j graph data
- Automate Confluence page creation for each service

---

## FINAL CHECKLIST BEFORE LEADERSHIP DEMO

```
Infrastructure
[ ] docker compose up -d runs cleanly
[ ] verify_services.sh shows all PASS
[ ] Open WebUI accessible from developer workstations
[ ] Sourcegraph connected to all target repos and indexing complete
    (check http://localhost:7080/site-admin/repositories)

Indexing
[ ] At least 5 target repos indexed in Qdrant
[ ] Neo4j has Service, Queue, Endpoint nodes with relationships
[ ] index_diff.py tested — push a change, verify it re-indexes

Q&A
[ ] /health endpoint returns all OK
[ ] /ask returns answers with sources for all 4 test queries
[ ] Response time under 30 seconds per question

Demo scenarios
[ ] Demo 1: payment flow question — answer mentions multiple repos
[ ] Demo 2: RabbitMQ publisher/consumer — answer traces publisher to consumer
[ ] Demo 3: push a change, show docs update within 5 minutes

Documentation
[ ] docs/services/ has at least one auto-generated service page
[ ] DECISIONS.md is fully filled in for all four phases
[ ] README.md is accurate and up to date
```

---

## CODER AGENT INSTRUCTIONS

- Read ALL sections of DECISIONS.md before writing anything
- The GitHub Actions workflow must use self-hosted runner — not ubuntu-latest
- The OpenAI-compatible wrapper in app.py is important — Open WebUI needs it
- Make demo_queries.sh pretty — this is what leadership will see
- After building everything, run the full checklist above with me
- Flag anything that is not working rather than hiding it

---

## Note on pre-existing scaffold files

`scripts/demo_queries.sh` already exists in this repo as a scaffold with the 3 demo scenarios.
When you build Phase 4, Copilot should review it, complete the RAG endpoint URL from DECISIONS.md,
and validate it runs correctly — not create from scratch.
