# What we are building

> Plain English. No jargon. Anyone on the team should be able to read this.

---

## The problem

We have 200+ software repositories. They were built over many years by many different people.
Nobody has a complete picture of how they all work together.

When a developer needs to understand something — like how a payment is processed,
or which service sends a RabbitMQ message — they have two options:

1. Ask a senior engineer (who might be busy, on leave, or have left the company)
2. Spend hours reading through dozens of files across multiple repos

Both options are slow and risky in a healthcare setting where systems cannot break.

---

## What we are building

An internal tool that lets any developer type a plain English question and get a clear answer
— with exact file references — in under 30 seconds.

Think of it like having a very knowledgeable colleague who has read every line of code
in every repository and is always available to answer questions.

The important thing: **this tool runs entirely inside our network.**
No code, no questions, and no answers ever go to the internet.

---

## Three things it can do

### 1. Answer questions about the codebase

Developers open a simple chat interface and type questions in plain English.
The system reads the relevant code, understands it, and gives a clear answer —
including which repository and file to look at.

Examples:
- *"How does the payment flow work end to end?"*
- *"Which service publishes to the RabbitMQ payment queue — and who consumes it?"*

---

### 2. Auto-generate documentation and architecture diagrams

The platform reads the code and automatically writes documentation:
service summaries, API endpoint lists, data flow diagrams, and architecture maps.
These are **living documents** — whenever a developer merges code, the documentation
updates itself within minutes.

No more stale wikis. No more asking someone to "update the docs" — it happens automatically.
Documentation can be written to MinIO and optionally pushed to Confluence via its REST API.

---

### 3. Tell you what will break before you make a change

You type: *"If I change PaymentService, what other services might be affected?"*

It traces every connection — which services call PaymentService,
which queues it publishes to, who consumes those queues —
and gives you a list of everything that could be impacted.

---

## How it stays up to date

Every time a developer merges code into the main branch,
an automated process runs within minutes:

1. Reads the changed files
2. Updates the knowledge store
3. Regenerates the documentation for affected services

There is nothing to maintain manually. The documentation updates itself.

---

## What does a developer actually see?

A simple web interface — accessible inside the company network — with a chat window.
They type a question. They get an answer with file links.
They can also browse auto-generated documentation pages for each service.
That is the entire user experience.

---

## Who uses it

| Person | What they use it for |
|--------|---------------------|
| New developer | Ramp up in days instead of months — understand services without asking anyone |
| Developer making a change | Check what else might break before touching shared code |
| Senior engineer | Stop being the single point of knowledge — answer questions without being interrupted |
| Team lead | Understand impact of changes before a sprint starts |
| Architect | Always-current architecture diagrams without manual effort |

---

## What it does not do

- It does not write code for you
- It does not access patient data or any production databases
- It does not replace code review
- It does not send anything outside our network — no calls to OpenAI, Google, or any external provider
- It is not a one-time project — it is a living system that improves as the codebase grows
