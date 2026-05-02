# Security and Healthcare Compliance

> Why this architecture is safe for a healthcare environment.

---

## The one rule everything else follows

**No data leaves our network.**

Not the code. Not the questions. Not the answers. Not the embeddings.
Every component runs on hardware we control.

---

## What data does this system handle?

This system only handles **source code** — not patient data.

| Data type | In this system? | Classification |
|-----------|----------------|----------------|
| Patient records (PHI) | No | N/A |
| Production database contents | No | N/A |
| Patient-facing application data | No | N/A |
| Source code (.java, .cs files) | Yes | Internal Confidential |
| Generated documentation | Yes | Internal |
| Developer questions | Yes | Internal |

Source code is treated as sensitive because it may contain:
- Database schema definitions that reveal data structure
- Connection string patterns
- Business logic that reveals clinical workflows

This is why we apply the same zero-egress requirement even though it is "just code."

---

## How we enforce zero data egress

### The AI model runs locally

Ollama downloads the model weights once, from a known source, with SHA-256 verification.
After that, the network connection for Ollama is severed.
All inference happens on our hardware. No query ever leaves.

Compare this to OpenAI or GitHub Copilot — every question you type is sent to their servers.
That is not acceptable for healthcare source code.

### The GitHub Actions runner is self-hosted

When code is pushed to GitHub, the Actions workflow runs on a **runner machine inside our network**.
GitHub only sends a "start job" trigger signal — the code itself is checked out locally
and never processed on GitHub's infrastructure.

### All databases are self-hosted

| Database | Runs on |
|----------|---------|
| Qdrant | Our EC2 / on-prem server |
| Neo4j | Our EC2 / on-prem server |
| MinIO | Our EC2 / on-prem server (or S3 in private VPC) |

No managed cloud database services. No SaaS vector databases.

### The UI is self-hosted

Open WebUI runs on our server. Developers access it via internal DNS or VPN.
It is not exposed to the public internet.

---

## Access control

**Developer access to the chat UI**
- Open WebUI supports SSO and LDAP integration
- Connect it to your existing Active Directory / identity provider
- Role-based access: restrict which teams can see which repo answers (Phase 2 enhancement)

**Service-to-service access**
- Qdrant: API key authentication, not exposed outside the server
- Neo4j: username/password, not exposed outside the server
- Ollama: not exposed outside the Docker network
- All secrets stored in environment variables, never in code

**GitHub integration**
- Fine-grained personal access token
- Read-only scope: `contents:read` for the target repos only
- No write access, no admin access, no secrets access

**Sourcegraph access**
- Sourcegraph is only accessible on the internal network (port 7080).
- It uses a read-only GitHub fine-grained PAT with `contents:read` scope only.
- The `SOURCEGRAPH_TOKEN` used by the indexer is a user-scoped API token
  generated inside Sourcegraph itself — it never touches GitHub credentials.

---

## Encryption

| What | How |
|------|-----|
| Data in transit (service to service) | TLS 1.2+ inside Docker network |
| Data in transit (developer to UI) | TLS via reverse proxy (nginx/Caddy) |
| Data at rest (MinIO/S3) | AES-256 server-side encryption |
| Data at rest (Sourcegraph) | `./sourcegraph_data` volume on server filesystem |
| Secrets (passwords, tokens) | Environment variables via .env file, not in Git |

---

## Audit logging

| Event | Where it is logged |
|-------|-------------------|
| Developer question and answer | Open WebUI built-in audit log |
| Indexing run (what repos, when) | GitHub Actions run history |
| File access in MinIO/S3 | MinIO access log / AWS CloudTrail |
| Neo4j queries | Neo4j query log (optional, enable if needed) |

---

## What to tell your security team

If your security team asks about this system, here are the key facts:

1. **No external AI APIs.** Ollama runs on our own hardware. DeepSeek Coder model weights are stored locally.

2. **No patient data is indexed.** The indexer only reads `.java` and `.cs` source code files. It does not connect to production databases.

3. **The GitHub token is read-only.** It has `contents:read` scope on specific repos only.

4. **Self-hosted GitHub Actions runner.** Code is processed inside our network perimeter, not on GitHub's servers.

5. **All components are open-source.** The full stack can be audited: Qdrant (Apache 2.0), Neo4j Community (GPL v3), Ollama (MIT), Open WebUI (MIT), LangChain (MIT), tree-sitter (MIT), Sourcegraph OSS (Apache 2.0) — self-hosted, read-only GitHub token.

6. **No vendor lock-in.** If any component needs to be replaced for compliance reasons, it can be swapped without rebuilding the whole system.

---

## Model supply chain

Model weights for Ollama (DeepSeek Coder) are downloaded **once** during initial setup
from the Ollama model registry. Before the model is put into service:

1. The download is performed on the target server inside our network
2. The model integrity is verified via SHA-256 hash against the published registry value
3. After verification, the outbound network connection for the Ollama container is severed
4. All production inference runs fully air-gapped — no query ever reaches the internet

This process is documented in `docs/05-how-to-run.md` and must be repeated
if the model is upgraded or replaced.

---

## Alternatives considered and rejected

| Alternative | Why rejected |
|---|---|
| OpenAI API (GPT-4) | Sends source code to external servers — unacceptable for Internal Confidential data |
| GitHub Copilot for codebase Q&A | Every query sent to Microsoft/OpenAI infrastructure |
| Azure OpenAI (private endpoint) | Data still leaves our network boundary to Azure; requires vendor BAA |
| HuggingFace Inference API | External API call with code content in the request body |
| Managed Qdrant Cloud | Vector embeddings of source code stored on third-party servers |
| Neo4j AuraDB | Relationship graph of internal service architecture stored externally |

In every rejected case the deciding factor was the same: data egress.
Once source code or its embeddings leave our network, we cannot guarantee they
are not retained, logged, or used to train future models.

---

## HIPAA considerations

This system does not directly handle PHI. However, because it indexes healthcare application code:

- Treat all indexed content as Internal Confidential
- Apply the same access controls you would to source code repositories
- Include this system in your annual security review
- Document it in your asset inventory

For formal HIPAA Business Associate Agreement (BAA) purposes:
because no PHI flows through this system, a BAA is not required for the system itself.
However, if your organisation requires BAAs for all internal tools, consult your compliance team.
