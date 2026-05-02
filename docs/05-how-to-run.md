# How to Run — Operational Runbook

> Step-by-step instructions for setting up and operating the platform.
> Written for a DevOps engineer who has not seen the system before.

---

## Prerequisites

Before you start, you need:

- [ ] An EC2 instance (r6i.2xlarge or better — 8 vCPU, 64 GB RAM) or equivalent on-prem server
- [ ] Docker and Docker Compose installed on the server
- [ ] A self-hosted GitHub Actions runner registered to your GitHub organisation
- [ ] A GitHub fine-grained personal access token (read-only, scoped to target repos)
- [ ] This repository cloned onto the server
- [ ] Ports 3000, 6333, 7474, 8000, 9000, 9001, 11434 open on the internal network firewall

---

> **Working directory:** All commands in this runbook must be run from the **repo root** — the directory that contains `infrastructure/`, `indexer/`, `rag/`, and `scripts/`.
> ```bash
> cd /path/to/codebase-intelligence
> ```
> Do not `cd` into a subdirectory before running a command unless the step explicitly tells you to.

---

## Step 1 — Configure environment variables

```bash
cp infrastructure/.env.example infrastructure/.env
```

Edit `infrastructure/.env` and fill in:
- Strong passwords for Neo4j and MinIO
- Your server's internal IP address
- Your GitHub token

**Never commit `infrastructure/.env` to Git.**

---

## Step 2 — Start all services

```bash
docker compose -f infrastructure/docker-compose.yml up -d
```

Wait 60 seconds for all services to initialise, then verify:

```bash
bash scripts/verify_services.sh
```

You should see PASS for all services. If any fail, check:
```bash
docker compose -f infrastructure/docker-compose.yml logs [service-name]
```

---

## Step 3 — Pull the AI model (one-time)

```bash
docker exec ollama ollama pull deepseek-coder:6.7b
```

This downloads ~4 GB. Do it once — the model is stored in the `./ollama_models` volume.
After this, Ollama does not need internet access.

Verify the model works:
```bash
curl -s http://localhost:11434/api/generate \
  -d '{"model":"deepseek-coder:6.7b","prompt":"What is a Java interface?","stream":false}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['response'][:200])"
```

---

## Step 4 — Index your first repo

Choose one small Java or .NET repo to test with.

```bash
pip install -r indexer/requirements.txt

# Clone the test repo somewhere on the server
git clone https://github.com/YOUR_ORG/test-repo /tmp/test-repo

# Run the indexer
python indexer/index_repos.py --repo /tmp/test-repo --name test-repo
```

Watch the output — it should show progress and a final summary.

Verify in Qdrant:
```bash
curl http://localhost:6333/collections/codebase-index
```

Verify in Neo4j — open http://YOUR_SERVER_IP:7474 and run:
```cypher
MATCH (n) RETURN n LIMIT 25
```

---

## Step 5 — Start the Q&A API

```bash
pip install -r rag/requirements.txt
uvicorn rag.app:app --host 0.0.0.0 --port 8000
```

Or run it in Docker (add it to docker-compose.yml for production).

Test it:
```bash
curl -s -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "What services are in the codebase?"}' \
  | python3 -m json.tool
```

---

## Step 6 — Configure Open WebUI

Open http://YOUR_SERVER_IP:3000 in your browser.

1. Create an admin account on first login
2. Go to Settings → Connections
3. Add a connection to `http://YOUR_SERVER_IP:8000`
4. Test with a question about your indexed repo

---

## Step 7 — Index all target repos

For the prototype, index 5–8 repos covering different domains:

```bash
# Run for each repo
python indexer/index_repos.py --repo /path/to/repo --name repo-name
```

For production (all 200+ repos), use the GitHub Actions pipeline — see Step 8.

---

## Step 8 — Set up the auto-indexing pipeline

1. Copy `.github/workflows/index-on-push.yml` to your target repos
   (or add it at organisation level as a reusable workflow)

2. Set these secrets in GitHub (organisation level):
   - `QDRANT_HOST` — your server IP
   - `QDRANT_PORT` — 6333
   - `NEO4J_URI` — bolt://YOUR_SERVER_IP:7687
   - `NEO4J_USER` — neo4j
   - `NEO4J_PASSWORD` — your password
   - `OLLAMA_HOST` — http://YOUR_SERVER_IP:11434

3. Push a small change to a .java or .cs file in a target repo and verify
   the Actions workflow runs and completes successfully.

---

## Step 9 — Generate service documentation

Prerequisites: Steps 1–5 complete and at least one repo indexed in Neo4j.

Add these variables to your `infrastructure/.env` file:

```
MINIO_ENDPOINT=localhost:9000
MINIO_ACCESS_KEY=<your MinIO access key>
MINIO_SECRET_KEY=<your MinIO secret key>
MINIO_SECURE=false
```

Run the doc generator:

```bash
python indexer/generate_docs.py
```

This defaults to `--output-dir docs/services/` and `--overview-file docs/architecture-overview.md` — both paths are locked in DECISIONS.md. Pass them explicitly only if you need to override.

Verify:
```bash
# Per-service Markdown files
ls docs/services/

# Architecture overview (Mermaid graph LR diagram)
cat docs/architecture-overview.md

# Objects in MinIO — open in browser
# http://YOUR_SERVER_IP:9001  (bucket: service-docs)
```

**Optional: push to Confluence**

Set these additional variables in `infrastructure/.env` — leave unset to skip:

```
CONFLUENCE_URL=http://YOUR_INTERNAL_CONFLUENCE:8090
CONFLUENCE_TOKEN=<your Confluence API token>
CONFLUENCE_SPACE_KEY=<your space key e.g. PLAT>
```

> **Important:** `CONFLUENCE_URL` must point to your **internal** Confluence instance.
> Never set it to Atlassian Cloud — that would send source code metadata outside the network.

**Optional: commit generated docs to the repo**

```
COMMIT_DOCS_TO_REPO=true
```

When set, `generate_docs.py` runs `git add / commit / push` with `[skip ci]` in the commit message so the indexing workflow is not triggered recursively.

---

## Step 10 — Run the leadership demo

Prerequisites: Steps 1–9 complete — all services running, repos indexed, docs generated.

```bash
bash scripts/demo_queries.sh
```

The script runs three scenarios in sequence:

1. **Payment flow** — `"How does the payment flow work end to end?"` — tests full RAG + multi-repo sources
2. **RabbitMQ trace** — `"Where is the RabbitMQ publisher for payment events and who consumes it?"` — tests graph context
3. **Impact analysis** — `"If I change PaymentService, what other services might be affected?"` — tests dependency traversal

Each answer takes **15–30 seconds** on CPU-only Ollama. With a GPU (p3.2xlarge) expect ~3 seconds.

What to look for in the output:
- Answer text with specific class and method references
- `Sources:` block listing `repo → file (lines N-M)` for each supporting chunk
- `(service dependency graph also used)` on queries where Neo4j graph context was applied

---

## Daily operations

### Check system health
```bash
curl http://localhost:8000/health
```

### Check how many items are indexed
```bash
curl http://localhost:8000/stats
```

### Re-index a single repo (if needed)
```bash
python indexer/index_repos.py --repo /path/to/repo --name repo-name
```

### View indexing logs
```bash
# GitHub Actions history for auto-indexing
# Or check the output of the last manual run
```

### Restart all services
```bash
docker compose -f infrastructure/docker-compose.yml restart
```

---

## Troubleshooting

| Problem | What to check |
|---------|--------------|
| verify_services.sh shows FAIL for Ollama | `docker logs ollama` — usually a port conflict |
| Qdrant has 0 vectors after indexing | Check indexer logs for tree-sitter parse errors |
| /ask returns empty sources | Check the embedding model name matches between indexer and RAG |
| Open WebUI shows "connection refused" | Check the FastAPI server is running on port 8000 |
| GitHub Actions workflow fails | Check the self-hosted runner is online and connected |
| Neo4j browser shows no nodes | Run indexer again — Neo4j writer may have failed silently |
| `generate_docs.py` exits with "Neo4j is required" | `NEO4J_PASSWORD` not set in `infrastructure/.env`, or Neo4j container not running — run `docker compose -f infrastructure/docker-compose.yml logs neo4j` |
| MinIO upload warnings in `generate_docs.py` output | `MINIO_ACCESS_KEY` or `MINIO_SECRET_KEY` missing from `infrastructure/.env`, or MinIO not running — run `docker compose -f infrastructure/docker-compose.yml logs minio` |

---

## Stopping the system

```bash
docker compose -f infrastructure/docker-compose.yml down
```

Data is preserved in Docker volumes. Start again with `docker compose up -d`.

To wipe all data and start fresh:
```bash
docker compose -f infrastructure/docker-compose.yml down -v
```
