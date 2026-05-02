# Phase 2 — Indexing Pipeline
## How to use this file
Attach this file with `#PHASE2-indexer.md` in Copilot Chat when using the **Coder** agent.
The Coder agent reads this file to know exactly what to build in this phase.

---

## CONTEXT — read this first

We are building a code indexing pipeline for a healthcare company's private codebase intelligence platform.

**Read DECISIONS.md Phase 1 section first.** Use the exact ports, service names, and collection names already decided there.

Stack already running (from Phase 1):
- Qdrant on port 6333
- Neo4j on port 7687
- Ollama on port 11434 with deepseek-coder:6.7b

Repos we are indexing: Java (.java files) and .NET C# (.cs files)
Number of repos: 200+ eventually, but we test on 2-3 repos first

---

## YOUR GOAL FOR THIS PHASE

Build a Python indexing pipeline that:
1. Reads source code files from a cloned repo
2. Parses them using tree-sitter to extract meaningful chunks
3. Calls Ollama locally to generate a plain-English summary of each chunk
4. Stores the chunk + embedding + summary in Qdrant
5. Extracts service relationships and writes them to Neo4j

Produce these files:
- `indexer/requirements.txt`
- `indexer/requirements.txt`
- `indexer/parsers/java_parser.py`
- `indexer/parsers/dotnet_parser.py`
- `indexer/clients/sourcegraph_client.py`
- `indexer/writers/qdrant_writer.py`
- `indexer/writers/neo4j_writer.py`
- `indexer/index_repos.py` (full run — indexes all files in a repo)
- `indexer/index_diff.py` (incremental — indexes only changed files, used by CI)

**Note on folder structure:**
- `indexer/parsers/` — language parsers (`java_parser.py`, `dotnet_parser.py`)
- `indexer/clients/` — external service clients (`sourcegraph_client.py`)
- `indexer/writers/` — database writers (`qdrant_writer.py`, `neo4j_writer.py`)

---

## DETAILED REQUIREMENTS

### requirements.txt must include:
```
tree-sitter>=0.21.0,<0.22.0
tree-sitter-java>=0.21.0,<0.22.0
tree-sitter-c-sharp>=0.21.0,<0.22.0
qdrant-client>=1.7.0,<2.0.0
neo4j>=5.0.0,<6.0.0
ollama>=0.1.0,<1.0.0
python-dotenv>=1.0.0,<2.0.0
gitpython>=3.1.0,<4.0.0
```

Note: LangChain is NOT needed for Phase 2 — the indexer calls Ollama directly via the ollama library.
LangChain is only needed in Phase 3 (the RAG chain). Do not add it here.

### java_parser.py must:
- Use tree-sitter to parse .java files
- Extract: method declarations, class declarations, interface declarations
- For each chunk return: { name, type, file, lines, raw_code, detected_patterns }
  - detected_patterns is a dict: { "rabbitmq": bool, "http": bool, "database": bool, "db_type": str|None }

**Pattern detection strategy — import + AST combined (~95% accuracy):**
- Step 1: Parse `import_declaration` AST nodes to build an imports set for the file
- Step 2: Match patterns against AST code nodes only (not comments or string literals)
- Step 3: A pattern is detected if found in imports OR in code body. Import-only = medium confidence, both = high confidence. Both are treated as detected.

- Import-level RabbitMQ markers: `org.springframework.amqp`, `com.rabbitmq`
- Import-level HTTP markers: `org.springframework.web`, `feign.`, `javax.ws.rs`
- Import-level DB markers: `javax.persistence`, `org.springframework.data`, `java.sql`, `org.springframework.jdbc`
- Import-level PostgreSQL markers: `org.postgresql`
- Body-level RabbitMQ patterns: `rabbitTemplate`, `@RabbitListener`, `channel.basicPublish`, `convertAndSend`
- Body-level HTTP patterns: `RestTemplate`, `WebClient`, `FeignClient`, `@GetMapping`, `@PostMapping`, `@RequestMapping`
- Body-level SQL/DB patterns: `@Repository`, `JdbcTemplate`, `EntityManager`, `DataSource`, `@Query`, `JpaRepository`
  - If detected, set db_type to "sql" (default) or "postgresql" (if PostgreSQL import found)

### dotnet_parser.py must:
- Use tree-sitter to parse .cs files
- Extract: method declarations, class declarations, interface declarations
- For each chunk return: { name, type, file, lines, raw_code, detected_patterns }
  - detected_patterns is a dict: { "rabbitmq": bool, "http": bool, "database": bool, "db_type": str|None }

**Pattern detection strategy — import + AST combined (~95% accuracy):**
- Step 1: Parse `using_directive` AST nodes to build a usings set for the file
- Step 2: Match patterns against AST code nodes only (not comments or string literals)
- Step 3: A pattern is detected if found in usings OR in code body.

- Using-level RabbitMQ markers: `RabbitMQ.Client`
- Using-level HTTP markers: `System.Net.Http`, `Microsoft.AspNetCore`
- Using-level DB markers: `System.Data`, `Microsoft.EntityFrameworkCore`
- Using-level PostgreSQL markers: `Npgsql`
- Body-level RabbitMQ patterns: `IModel`, `BasicPublish`, `EventingBasicConsumer`, `IRabbitMQService`
- Body-level HTTP patterns: `HttpClient`, `IHttpClientFactory`, `[HttpGet]`, `[HttpPost]`, `[Route]`, `[ApiController]`
- Body-level SQL/DB patterns: `DbContext`, `IRepository`, `SqlConnection`, `SqlCommand`, `NpgsqlConnection`, `IDbConnection`
  - If detected, set db_type to "sql" (default) or "postgresql" (if Npgsql found)

### qdrant_writer.py must:
- Connect to Qdrant using QDRANT_HOST and QDRANT_PORT from environment
- Create collection if it does not exist, with:
  - Collection name: "codebase-index"
  - Vector size: match the embedding model output size
  - Distance: Cosine
- For each chunk:
  - Call Ollama embed endpoint to get the vector
  - Upsert to Qdrant with payload: { repo, file, method, lines, language, summary, indexed_at }
- Handle upsert errors gracefully — log and continue, do not crash

### neo4j_writer.py must:
- Connect to Neo4j using NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD from environment
- Create or merge Service nodes: `MERGE (s:Service {name: $name, repo: $repo})`
- Create or merge Queue nodes: `MERGE (q:Queue {name: $name})`
- Create or merge Endpoint nodes: `MERGE (e:Endpoint {path: $path, method: $method})`
- Create or merge Database nodes: `MERGE (d:Database {name: $name, type: $type})`
  - type is "sql", "postgresql", or "rds" — from detected_patterns.db_type in the chunk
- Create relationships:
  - CALLS between services (from HTTP client detections)
  - PUBLISHES_TO from service to queue
  - CONSUMES_FROM from service to queue
  - EXPOSES from service to endpoint
  - READS_FROM from service to database — when method name matches: find|get|query|select|load|fetch|list
  - WRITES_TO from service to database — when method name matches: save|update|insert|delete|persist|create|put
  - If method name matches neither, create both READS_FROM and WRITES_TO (ambiguous)
- All writes use MERGE not CREATE — safe to re-run without duplicates

### index_repos.py must:
- Accept a repo path as a CLI argument: `python index_repos.py --repo /path/to/repo --name repo-name`
- Walk all .java and .cs files recursively
- Skip: test files (*/test/*, *Test.java, *Tests.cs), generated files (obj/, target/, bin/)
- Follow this exact indexing flow:
  - Step 1: tree-sitter parses each .java and .cs file
  - Step 2: Ollama summarises each chunk
  - Step 3: qdrant_writer.py stores chunks + embeddings in Qdrant
  - Step 4: neo4j_writer.py writes relationships found within the file
  - Step 5 (after all files): sourcegraph_client.py queries for cross-repo
    callers and adds those CALLS relationships to Neo4j
- Print progress: "Indexed 45/230 files..."
- Print final summary: time taken, files indexed, chunks created, errors

### index_diff.py must:
- Accept a list of changed files: `python index_diff.py --files file1.java file2.cs`
- Or detect changed files from git diff: `python index_diff.py --git-diff HEAD~1`
- Only index the changed files — not the whole repo
- Delete old Qdrant points for changed files before reinserting (use file path as filter)
- Same output as index_repos.py but for changed files only

---

## TESTING REQUIREMENTS

Before moving to Phase 3, verify:

```bash
# Test Sourcegraph connection before running full index
python -c "
from indexer.clients.sourcegraph_client import SourcegraphClient
client = SourcegraphClient()
results = client.search_symbol('PaymentService')
print(f'Sourcegraph connected. Found {len(results)} results.')
"

# Test on one small Java repo
python indexer/index_repos.py --repo /path/to/test-java-repo --name test-java

# Check Qdrant has data
curl http://localhost:6333/collections/codebase-index

# Check Neo4j has nodes
# Open http://localhost:7474 and run: MATCH (n) RETURN n LIMIT 25

# Test a quick semantic search using a real embedding from Ollama
python -c "
import ollama, os
from qdrant_client import QdrantClient

# Get a real embedding from Ollama (same model used during indexing)
model = os.getenv('OLLAMA_MODEL', 'deepseek-coder:6.7b')
response = ollama.embeddings(model=model, prompt='RabbitMQ payment publisher')
vector = response['embedding']

# Search Qdrant with the real vector
client = QdrantClient(os.getenv('QDRANT_HOST', 'localhost'), port=int(os.getenv('QDRANT_PORT', 6333)))
results = client.search(
    collection_name='codebase-index',
    query_vector=vector,
    limit=3
)
for r in results:
    print(r.payload['file'], r.payload['method'], round(r.score, 3))
"
```

---

## WHAT TO WRITE IN DECISIONS.md WHEN DONE

Append to the Phase 2 section:
- Qdrant collection name and vector size used
- Embedding model and its output dimensions
- Chunk strategy decisions (why method-level, any exceptions)
- Neo4j node and relationship types created
- Any RabbitMQ or HTTP patterns that needed adjustment for your actual codebase
- Index run time on your test repos

---

## CODER AGENT INSTRUCTIONS

- Read DECISIONS.md Phase 1 section before writing any connection code
- Use environment variables for all connection details — never hardcode
- Every function needs a docstring explaining what it does
- Add logging throughout — this pipeline runs in CI and needs to be debuggable
- If tree-sitter struggles with a file, log the error and skip — never crash the whole run
- After generating each file, explain the key design decisions in plain English
- Do not move to the next file until I confirm I understand the previous one
