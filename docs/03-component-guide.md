# Component Guide — What each tool does and why we chose it

> One section per component. Plain English. Includes why we chose it over alternatives.

---

## Sourcegraph — cross-repo code search

**What it does in one sentence:**
Google Search for your internal codebase — searches all 200+ repos instantly.

**The longer version:**
Sourcegraph indexes every file in every repo and gives you a search interface
that works across all of them at once. You can search by text, by symbol name,
by file type, or by regular expression. It also understands code structure —
you can ask "find all callers of this method" and it traces it across repos.

**Where it helps in this project:**
The initial discovery phase — finding all the repos that contain payment logic,
all the places RabbitMQ is used, all the controllers in a service.

**Why not just use GitHub search?**
GitHub's built-in search is limited — it doesn't do cross-repo symbol navigation
and it doesn't give you a programmatic API to query at scale.

---

## tree-sitter — code structure extraction

**What it does in one sentence:**
Reads Java and .NET code and extracts its structure — methods, classes, API routes — as data.

**The longer version:**
A code parser that understands the grammar of programming languages.
When it reads a Java file, it does not just see text — it understands that
`public void processPayment(Order order)` is a method declaration with a specific name,
return type, and parameters. This structured understanding is what lets us extract
"all RabbitMQ publishers" or "all REST API endpoints" automatically.

**Where it helps in this project:**
Splitting code into meaningful chunks for the AI, and extracting relationship data
(service calls, queue publishers, API routes) for Neo4j.

**Why not just split by line count?**
Splitting at arbitrary line counts would break methods in half, making the AI
summaries incoherent. Splitting at method boundaries means each chunk is a
complete, understandable unit.

---

## Ollama + DeepSeek Coder — the local AI

**What it does in one sentence:**
A private version of ChatGPT that runs on your own server and specialises in understanding code.

**The longer version:**
Ollama is the server — it loads AI models and serves them via an API on your machine.
DeepSeek Coder is the model loaded into it — it was trained specifically on code
and is good at understanding, summarising, and explaining programming concepts.

When the indexer processes a method, it calls Ollama to write a plain English summary.
When a developer asks a question, it calls Ollama to write the answer.

**Where it helps in this project:**
Every AI-powered step — summarising code during indexing and answering questions at query time.

**The critical point for healthcare:**
After the model is downloaded once during setup, the network connection can be severed.
All inference runs completely locally — no query ever reaches the internet.
This is non-negotiable for HIPAA compliance.

**Why DeepSeek Coder and not CodeLlama?**
Both are good. DeepSeek Coder generally performs better on Java and .NET understanding
in benchmarks as of early 2026. You can swap models by changing one line in the config.

---

## Qdrant — semantic search database

**What it does in one sentence:**
A database that stores knowledge by meaning, so "RabbitMQ publisher" finds
`dispatchPaymentEvent()` even though the words are completely different.

**The longer version:**
Qdrant stores each code chunk as a vector — a list of ~768 numbers that represents
the meaning of that chunk. When you search, your question is also converted to a vector,
and Qdrant finds the stored vectors that are mathematically closest.
Closeness in vector space = similarity in meaning.

**Where it helps in this project:**
Answering "what is X", "how does X work", "where is the code for X" questions
where the developer does not know the exact method or class name.

**Why not Elasticsearch?**
Elasticsearch does keyword search — it finds documents that contain your search words.
If you search "payment publisher" it will not find `dispatchPaymentEvent()`.
Qdrant finds it because both phrases have similar meaning in vector space.

---

## Neo4j — service dependency graph

**What it does in one sentence:**
A database shaped like a web of connections — stores which services talk to which,
which publish to which queues, and which consume from which.

**The longer version:**
Neo4j stores data as nodes (things) and edges (connections between things).
A node might be "PaymentService" and an edge might be "CALLS" pointing to "OrderService".
This structure makes it natural to ask questions like
"start at PaymentService and follow all CALLS edges — what can you reach?"

**Where it helps in this project:**
Impact analysis — "if I change PaymentService, what else might break?" — is answered
by traversing the graph. It is also used to trace publisher-to-consumer chains
for RabbitMQ questions.

**Why not just use Qdrant for this?**
Qdrant finds similar things. Neo4j finds connected things. They are different questions.
"What services call PaymentService?" is a graph question, not a similarity question.

---

## MinIO / S3 — document storage

**What it does in one sentence:**
A file storage system for the auto-generated documentation and architecture diagrams.

**The longer version:**
After the indexer generates Markdown docs for each service,
they need to be stored somewhere the UI can serve them.
MinIO is a self-hosted file store that works exactly like Amazon S3.
If you are already in AWS, you can use S3 in your private VPC instead.

**Where it helps in this project:**
Storing and versioning the auto-generated service documentation pages.
Can also sync to Confluence via the Confluence REST API.

**Is this needed for the prototype?**
No — you can skip MinIO in Week 1 and just write docs to the local filesystem.
Add MinIO in Week 2 when you build the doc generator.

---

## LangChain — the orchestration layer

**What it does in one sentence:**
The glue that connects Qdrant, Neo4j, and Ollama into a single question-answering pipeline.

**The longer version:**
LangChain provides ready-made components for building retrieval-augmented generation (RAG) pipelines.
Instead of writing all the wiring code yourself, LangChain gives you building blocks:
"retrieve from Qdrant", "query Neo4j", "call Ollama", "format the prompt".

**Where it helps in this project:**
Building the chain.py file in Phase 3. Without LangChain you could still build this,
but you would write more boilerplate code.

---

## Open WebUI — the chat interface

**What it does in one sentence:**
The web page developers open to ask questions — looks and feels like ChatGPT
but connects to your local AI.

**The longer version:**
Open WebUI is a self-hosted web application that provides a polished chat interface.
It connects to any Ollama-compatible backend, supports SSO and LDAP,
has audit logs, and supports multiple users with different access levels.

**Where it helps in this project:**
The developer-facing front door to the whole system.
Developers do not need to know anything about the underlying stack —
they just open a URL in their browser and start asking questions.

---

## Mermaid.js — auto-generated diagrams

**What it does in one sentence:**
Turns a text description of a diagram into an actual diagram — arrows, boxes, and all.

**The longer version:**
Instead of drawing diagrams in a tool like Lucidchart, the AI writes a text description
in Mermaid syntax (e.g. `ServiceA --> ServiceB`) and Mermaid renders it as a visual diagram.
Because the diagram is text, it lives in Git alongside the code and versions with it.

**Where it helps in this project:**
Auto-generating architecture diagrams for each service page in the documentation.
When the indexer updates a service's relationships, it regenerates the Mermaid diagram too.

---

## GitHub Actions (self-hosted runner) — the auto-update pipeline

**What it does in one sentence:**
Every time code is merged, automatically re-indexes the changed files
so the documentation never goes stale.

**The self-hosted part is critical:**
A self-hosted runner is a machine inside your network that GitHub Actions sends jobs to.
The code never leaves your network — GitHub just sends a "run this job" signal,
and your runner executes it locally.

**Where it helps in this project:**
Keeping everything current without anyone manually running the indexer.
This is what turns the project from a one-time analysis tool into a living system.
