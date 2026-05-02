# Phase 1 — Infrastructure Setup
## How to use this file
Attach this file with `#PHASE1-infrastructure.md` in Copilot Chat when using the **Coder** agent.
The Coder agent reads this file to know exactly what to build in this phase.

---

## CONTEXT — read this first

We are building a private codebase intelligence platform for a healthcare company.
200+ repos in Java and .NET, hosted on GitHub.
Everything runs on-premise or in a private AWS VPC.
No data leaves our network. No external AI APIs.

The stack we are deploying:
- Qdrant (vector database for semantic search)
- Neo4j Community (graph database for service dependencies)
- Ollama (local LLM inference — no internet)
- Open WebUI (developer chat interface)
- MinIO (object storage for generated docs)

Read DECISIONS.md before you start. If Phase 1 decisions are already filled in, use those exact values.

---

## PREREQUISITES — confirm before writing any code

The following must be in place before Phase 1 can start:

- [ ] EC2 instance (r6i.2xlarge or equivalent — 8 vCPU, 64 GB RAM minimum for Ollama + all services)
- [ ] Docker and Docker Compose installed on the server
- [ ] Self-hosted GitHub Actions runner registered to the organisation
- [ ] GitHub fine-grained personal access token — read-only, `contents:read` scope on target repos
- [ ] 5–8 target repos identified (e.g. payment, order, notification, messaging services)
- [ ] Internal DNS entry or server IP for developer access to the UI
- [ ] Ports 3000, 6333, 7474, 7687, 8000, 9000, 9001, 11434 open on the internal network firewall

If any of these are missing, flag it to the user before proceeding.

---

## YOUR GOAL FOR THIS PHASE

Produce the following files, ready to run:

1. `infrastructure/docker-compose.yml`
2. `infrastructure/.env.example`
3. `scripts/verify_services.sh`

The stack must start cleanly with `docker compose up -d` and all services must pass the verify script.

---

## DETAILED REQUIREMENTS

### docker-compose.yml must include:

**Qdrant**
- Image: qdrant/qdrant:latest
- Ports: 6333 (HTTP API), 6334 (gRPC)
- Volume: ./qdrant_data:/qdrant/storage
- Restart: always

**Neo4j Community**
- Image: neo4j:5-community
- Ports: 7474 (browser UI), 7687 (bolt protocol)
- Volume: ./neo4j_data:/data
- Environment: NEO4J_AUTH from .env
- Restart: always

**Ollama**
- Image: ollama/ollama:latest
- Port: 11434
- Volume: ./ollama_models:/root/.ollama
- Restart: always
- Note: GPU passthrough optional — comment it out but include it as a comment
- GPU spec note: if GPU is available, add `deploy.resources.reservations.devices` for NVIDIA.
  Recommended upgrade instance: p3.2xlarge gives ~10× faster inference (add to DECISIONS.md Phase 1 section)

**Open WebUI**
- Image: ghcr.io/open-webui/open-webui:main
- Port: 3000 (maps to internal 8080)
- Environment: OLLAMA_BASE_URL=http://ollama:11434
- Depends on: ollama
- Volume: ./openwebui_data:/app/backend/data
- Restart: always

**MinIO**
- Image: minio/minio:latest
- Port: 9000 (API), 9001 (console)
- Volume: ./minio_data:/data
- Environment: MINIO_ROOT_USER and MINIO_ROOT_PASSWORD from .env
- Command: server /data --console-address ":9001"
- Restart: always

All services must be on the same Docker network named: `codebase-net`

### .env.example must include all of these — every variable used anywhere in the stack:
- SERVER_IP=YOUR_SERVER_IP
- NEO4J_AUTH=neo4j/CHANGE_ME_STRONG_PASSWORD
- NEO4J_URI=bolt://localhost:7687
- NEO4J_USER=neo4j
- NEO4J_PASSWORD=CHANGE_ME_STRONG_PASSWORD
- QDRANT_HOST=localhost
- QDRANT_PORT=6333
- QDRANT_API_KEY=CHANGE_ME_OR_LEAVE_BLANK
- OLLAMA_HOST=http://localhost:11434
- OLLAMA_MODEL=deepseek-coder:6.7b
- MINIO_ROOT_USER=admin
- MINIO_ROOT_PASSWORD=CHANGE_ME_STRONG_PASSWORD
- MINIO_ENDPOINT=http://localhost:9000
- GITHUB_TOKEN=github_pat_CHANGE_ME

### verify_services.sh must:
- curl Qdrant health endpoint (port 6333)
- curl Ollama health endpoint (port 11434)
- curl Neo4j HTTP endpoint (port 7474)
- curl MinIO health endpoint (port 9000)
- curl Sourcegraph health endpoint (port 7080)
- Print PASS or FAIL for each
- Print final summary line

---

## AFTER BUILDING

1. Run: `docker compose -f infrastructure/docker-compose.yml up -d`
2. Run: `bash scripts/verify_services.sh`
3. Pull the model: `docker exec ollama ollama pull deepseek-coder:6.7b`
4. Verify Ollama works: `curl http://localhost:11434/api/generate -d '{"model":"deepseek-coder:6.7b","prompt":"What is a Java method?","stream":false}'`
5. Open Neo4j browser: http://localhost:7474
6. Open Open WebUI: http://localhost:3000
7. Open Sourcegraph at http://localhost:7080, complete the setup wizard,
   connect it to GitHub using your GITHUB_TOKEN, and add the 5-8 target repos.
   Wait for Sourcegraph to finish indexing before starting Phase 2.

---

## WHAT TO WRITE IN DECISIONS.md WHEN DONE

Append to the Phase 1 section:
- Exact port mappings used
- Volume paths
- Model chosen and pull command
- Any changes you made from the spec above and why

---

## CODER AGENT INSTRUCTIONS

- Write clean, production-quality code with comments explaining non-obvious choices
- Use .env variables — never hardcode passwords
- Include health checks in the Docker Compose services
- If you encounter a conflict or ambiguity, ask me before proceeding
- After generating each file, explain what it does in 2-3 sentences so I understand it
- Do not move on until I confirm each file works
- Sourcegraph takes 2-3 minutes to fully start — the health check has
  a 120s start_period to account for this

---

## Note on pre-existing scaffold files

`infrastructure/docker-compose.yml`, `infrastructure/.env.example`, and `scripts/verify_services.sh`
already exist in this repo as scaffolds. When you build Phase 1, Copilot should
review these files, complete any missing sections, and validate them — not create from scratch.
