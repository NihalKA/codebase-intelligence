"""
indexer/writers/neo4j_writer.py
--------------------------------
Writes service relationship data extracted by the parsers into Neo4j.

For each parser chunk the writer inspects ``detected_patterns`` and creates
or merges the appropriate nodes and relationships using DECISIONS.md-locked
node types and relationship types:

  Node types (exact case, per DECISIONS.md):
    Service   { name, repo }
    Queue     { name }
    Endpoint  { path, method }
    Database  { name, type }   — type: "sql" | "postgresql" | "rds"

  Relationship types (per DECISIONS.md):
    (Service)-[:CALLS]->(Service)
    (Service)-[:PUBLISHES_TO]->(Queue)
    (Service)-[:CONSUMES_FROM]->(Queue)
    (Service)-[:EXPOSES]->(Endpoint)
    (Service)-[:READS_FROM]->(Database)
    (Service)-[:WRITES_TO]->(Database)

All Cypher uses MERGE (not CREATE) so the writer is safe to re-run — it will
update existing nodes/edges, not create duplicates.

READS_FROM vs WRITES_TO heuristic (DECISIONS.md):
  WRITES_TO  — method name contains: save|update|insert|delete|persist|create|put
  READS_FROM — method name contains: find|get|query|select|load|fetch|list
  Ambiguous  — create both READS_FROM and WRITES_TO

Configuration (all from environment — never hardcoded):
  NEO4J_URI       Bolt URI     (default: bolt://localhost:7687)
  NEO4J_USER      Username     (default: neo4j)
  NEO4J_PASSWORD  Password     (required — writer disabled if unset)
"""

import logging
import os
import pathlib
import re
from typing import Optional

import neo4j
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# Path is resolved relative to this file so it works regardless of CWD.
load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent.parent.parent / "infrastructure" / ".env")

# ---------------------------------------------------------------------------
# READS_FROM vs WRITES_TO heuristic — word lists locked in DECISIONS.md.
# Compiled once at module load for efficiency across thousands of chunks.
# ---------------------------------------------------------------------------
_READ_PATTERN: re.Pattern = re.compile(
    r"\b(find|get|query|select|load|fetch|list)\b", re.IGNORECASE
)
_WRITE_PATTERN: re.Pattern = re.compile(
    r"\b(save|update|insert|delete|persist|create|put)\b", re.IGNORECASE
)

# Heuristic for RabbitMQ direction — method name keywords that indicate
# which side of the queue boundary a service is on.
_PUBLISH_PATTERN: re.Pattern = re.compile(
    r"\b(publish|send|producer|emit|dispatch)\b", re.IGNORECASE
)
_CONSUME_PATTERN: re.Pattern = re.compile(
    r"\b(listen|consume|receive|handler|subscriber|process)\b", re.IGNORECASE
)

# HTTP endpoint vs HTTP client — determines EXPOSES vs CALLS.
# Patterns that indicate the chunk *is* an endpoint (server-side controller).
_ENDPOINT_PATTERNS: frozenset[str] = frozenset(
    ["@GetMapping", "@PostMapping", "@PutMapping", "@DeleteMapping",
     "@RequestMapping", "[HttpGet]", "[HttpPost]", "[HttpPut]", "[HttpDelete]",
     "[Route]", "[ApiController]"]
)
# Patterns that indicate the chunk *calls* another service (client-side).
_HTTP_CLIENT_PATTERNS: frozenset[str] = frozenset(
    ["RestTemplate", "WebClient", "FeignClient", "HttpClient", "IHttpClientFactory"]
)


class Neo4jWriter:
    """
    Persists service relationship graph data into Neo4j.

    One instance is created per indexing run.  It holds an open Neo4j driver
    for the duration of the run.  If ``NEO4J_PASSWORD`` is unset the writer
    degrades gracefully — every write method becomes a no-op with a warning,
    ensuring the indexing run can still complete (Qdrant data is still written).

    Parameters
    ----------
    repo_name : str
        Short name of the repository being indexed, e.g. ``"payment-service"``.
        Stored on every Service node as the ``repo`` property.
    """

    def __init__(self, repo_name: str) -> None:
        self._repo_name: str = repo_name
        self._driver: Optional[neo4j.Driver] = None

        neo4j_uri: str = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        neo4j_user: str = os.environ.get("NEO4J_USER", "neo4j")
        neo4j_password: str = os.environ.get("NEO4J_PASSWORD", "")

        if not neo4j_password:
            # NEO4J_PASSWORD must come from the environment.  An unset password
            # means the caller has not configured this service — degrade
            # gracefully rather than crashing the entire indexing run.
            logger.warning(
                "NEO4J_PASSWORD is not set — Neo4j writer is disabled. "
                "Graph relationships will not be written for this run."
            )
            return

        try:
            self._driver = neo4j.GraphDatabase.driver(
                neo4j_uri, auth=(neo4j_user, neo4j_password)
            )
            # Verify connectivity immediately so a misconfigured URI surfaces
            # at startup rather than on the first write.
            self._driver.verify_connectivity()
            logger.info("Neo4j driver connected to %s", neo4j_uri)
        except Exception as exc:
            logger.error(
                "Failed to connect to Neo4j at %s: %s — "
                "graph writes will be skipped for this run.",
                neo4j_uri,
                exc,
            )
            # Set driver to None so all write methods degrade gracefully.
            self._driver = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """
        Close the Neo4j driver and release its connection pool.

        Should be called once the indexing run is complete.  Safe to call
        even if the driver was never opened (e.g. when NEO4J_PASSWORD was unset).
        """
        if self._driver is not None:
            try:
                self._driver.close()
                logger.info("Neo4j driver closed.")
            except Exception as exc:
                logger.warning("Error closing Neo4j driver: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ensure_service(self, service_name: str) -> None:
        """
        Create or merge a Service node in Neo4j.

        Uses MERGE so calling this multiple times for the same service is safe —
        it creates a single node, not duplicates.

        Parameters
        ----------
        service_name : str
            The canonical name of the service, e.g. ``"PaymentService"``.
        """
        query = (
            "MERGE (s:Service {name: $name, repo: $repo})"
        )
        self._run_query(query, {"name": service_name, "repo": self._repo_name})

    def write_relationships(self, chunk: dict, service_name: str) -> None:
        """
        Inspect a chunk's ``detected_patterns`` and write the appropriate
        Neo4j nodes and relationships.

        This is the primary entry point called by the indexer for every chunk.
        It first ensures the owning Service node exists, then branches on each
        detected pattern to create Queue, Database, or Endpoint nodes plus the
        corresponding relationships.

        Parameters
        ----------
        chunk : dict
            Parser chunk dict with keys ``name``, ``type``, ``raw_code``,
            ``detected_patterns`` (from ``java_parser`` or ``dotnet_parser``).
        service_name : str
            Name of the service that owns this chunk.
        """
        if self._driver is None:
            return

        # Always ensure the owning service node exists first.
        self.ensure_service(service_name)

        patterns: dict = chunk.get("detected_patterns", {})
        chunk_name: str = chunk.get("name", "unknown")
        raw_code: str = chunk.get("raw_code", "")

        # -- RabbitMQ relationships ----------------------------------------
        if patterns.get("rabbitmq"):
            self._write_queue_relationships(service_name, chunk_name, raw_code)

        # -- HTTP relationships (endpoints exposed + services called) --------
        if patterns.get("http"):
            self._write_http_relationships(service_name, chunk_name, raw_code)

        # -- Database relationships -----------------------------------------
        if patterns.get("database"):
            db_type: str = patterns.get("db_type") or "sql"
            self._write_database_relationships(service_name, chunk_name, db_type)

    def write_cross_repo_call(
        self,
        source_service: str,
        target_service: str,
        source_repo: str,
        target_repo: str,
    ) -> None:
        """
        Create a CALLS relationship between two services that live in different repos.

        Called by ``index_repos.py`` Step 5 after Sourcegraph returns cross-repo
        caller results.  Both Service nodes are merged so this is safe even if
        the target service has not been indexed yet.

        Parameters
        ----------
        source_service : str
            Name of the service making the call.
        target_service : str
            Name of the service being called.
        source_repo : str
            Repository name of the calling service.
        target_repo : str
            Repository name of the called service.
        """
        query = (
            "MERGE (src:Service {name: $source_name, repo: $source_repo}) "
            "MERGE (tgt:Service {name: $target_name, repo: $target_repo}) "
            "MERGE (src)-[:CALLS]->(tgt)"
        )
        self._run_query(
            query,
            {
                "source_name": source_service,
                "source_repo": source_repo,
                "target_name": target_service,
                "target_repo": target_repo,
            },
        )
        logger.debug(
            "Cross-repo CALLS: %s/%s → %s/%s",
            source_repo,
            source_service,
            target_repo,
            target_service,
        )

    # ------------------------------------------------------------------
    # Private relationship helpers
    # ------------------------------------------------------------------

    def _write_queue_relationships(
        self, service_name: str, method_name: str, raw_code: str = ""
    ) -> None:
        """
        Create Queue node and PUBLISHES_TO / CONSUMES_FROM relationships.

        Direction is determined from the method name:
          - Contains publish/send/producer/emit/dispatch → PUBLISHES_TO
          - Contains listen/consume/receive/handler/subscriber/process → CONSUMES_FROM
          - Ambiguous (matches neither or both) → create both relationships

        Queue name is derived as ``{service_name}-queue`` — a best-effort
        default.  The actual queue name would require string-literal extraction
        which is out of scope for Phase 2.

        Parameters
        ----------
        service_name : str
            The owning service node name.
        method_name : str
            The method name used to determine queue direction.
        """
        # Derive a consistent queue name from the service — actual queue names
        # require string-literal parsing which is not implemented in Phase 2.
        queue_name: str = f"{service_name}-queue"

        # Check both method name AND raw code body — eShop-style Handle() methods
        # carry publish/consume logic in the body, not the method name.
        is_publisher: bool = bool(_PUBLISH_PATTERN.search(method_name))
        is_consumer: bool = bool(_CONSUME_PATTERN.search(method_name))

        # Also scan the raw code body — catches PublishAsync/eventBus.Publish in Handle().
        if not is_publisher:
            is_publisher = bool(_PUBLISH_PATTERN.search(raw_code))
        if not is_consumer:
            is_consumer = bool(_CONSUME_PATTERN.search(raw_code))

        if not is_publisher and not is_consumer:
            # Ambiguous — create both so no relationship is missed.
            is_publisher = True
            is_consumer = True

        if is_publisher:
            query = (
                "MERGE (s:Service {name: $service_name, repo: $repo}) "
                "MERGE (q:Queue {name: $queue_name}) "
                "MERGE (s)-[:PUBLISHES_TO]->(q)"
            )
            self._run_query(
                query,
                {
                    "service_name": service_name,
                    "repo": self._repo_name,
                    "queue_name": queue_name,
                },
            )
            logger.debug(
                "PUBLISHES_TO: %s → %s (method: %s)", service_name, queue_name, method_name
            )

        if is_consumer:
            query = (
                "MERGE (s:Service {name: $service_name, repo: $repo}) "
                "MERGE (q:Queue {name: $queue_name}) "
                "MERGE (s)-[:CONSUMES_FROM]->(q)"
            )
            self._run_query(
                query,
                {
                    "service_name": service_name,
                    "repo": self._repo_name,
                    "queue_name": queue_name,
                },
            )
            logger.debug(
                "CONSUMES_FROM: %s ← %s (method: %s)", service_name, queue_name, method_name
            )

    def _write_http_relationships(
        self, service_name: str, method_name: str, raw_code: str
    ) -> None:
        """
        Create EXPOSES (endpoint) or CALLS (HTTP client) relationships.

        Distinguishes between:
          - Server-side controller methods → EXPOSES Endpoint
          - Client-side HTTP caller methods → CALLS Service (placeholder target)

        Detection is based on pattern keywords found in the raw code text.

        Parameters
        ----------
        service_name : str
            The owning service node name.
        method_name : str
            The method name (used as the endpoint path as a best-effort default).
        raw_code : str
            Raw source code of the chunk (scanned for HTTP pattern keywords).
        """
        has_endpoint_annotation: bool = any(
            pattern in raw_code for pattern in _ENDPOINT_PATTERNS
        )
        has_http_client: bool = any(
            pattern in raw_code for pattern in _HTTP_CLIENT_PATTERNS
        )

        if has_endpoint_annotation:
            # This chunk is an HTTP endpoint being exposed by the service.
            # Derive HTTP method from annotation patterns in raw_code.
            http_method: str = _infer_http_method(raw_code)
            # Use method name as path — actual route extraction requires
            # annotation argument parsing which is out of scope for Phase 2.
            endpoint_path: str = f"/{method_name}"

            query = (
                "MERGE (s:Service {name: $service_name, repo: $repo}) "
                "MERGE (e:Endpoint {path: $path, method: $http_method}) "
                "MERGE (s)-[:EXPOSES]->(e)"
            )
            self._run_query(
                query,
                {
                    "service_name": service_name,
                    "repo": self._repo_name,
                    "path": endpoint_path,
                    "http_method": http_method,
                },
            )
            logger.debug(
                "EXPOSES: %s → %s %s", service_name, http_method, endpoint_path
            )

        if has_http_client:
            # This chunk calls another service over HTTP.  The exact target
            # service name would require resolving the client's base URL, which
            # is out of scope for Phase 2.  Sourcegraph (Step 5) provides
            # more accurate CALLS edges; this is a fallback placeholder.
            placeholder_target: str = "unknown-service"
            query = (
                "MERGE (src:Service {name: $source_name, repo: $repo}) "
                "MERGE (tgt:Service {name: $target_name}) "
                "MERGE (src)-[:CALLS]->(tgt)"
            )
            self._run_query(
                query,
                {
                    "source_name": service_name,
                    "repo": self._repo_name,
                    "target_name": placeholder_target,
                },
            )
            logger.debug(
                "CALLS (placeholder): %s → %s (method: %s)",
                service_name,
                placeholder_target,
                method_name,
            )

    def _write_database_relationships(
        self, service_name: str, method_name: str, db_type: str
    ) -> None:
        """
        Create Database node and READS_FROM / WRITES_TO relationships.

        READS_FROM vs WRITES_TO is determined by the DECISIONS.md heuristic:
          WRITES_TO  if method name matches: save|update|insert|delete|persist|create|put
          READS_FROM if method name matches: find|get|query|select|load|fetch|list
          Both       if method name matches neither pattern (ambiguous)

        Parameters
        ----------
        service_name : str
            The owning service node name.
        method_name : str
            The method name — used to determine read vs write intent.
        db_type : str
            One of ``"sql"``, ``"postgresql"``, or ``"rds"``.
        """
        # Derive a database node name — actual DB names require config/property
        # file parsing which is out of scope for Phase 2.
        db_name: str = f"{service_name}-db"

        is_read: bool = bool(_READ_PATTERN.search(method_name))
        is_write: bool = bool(_WRITE_PATTERN.search(method_name))

        if not is_read and not is_write:
            # Ambiguous — create both per DECISIONS.md specification.
            is_read = True
            is_write = True

        if is_write:
            query = (
                "MERGE (s:Service {name: $service_name, repo: $repo}) "
                "MERGE (d:Database {name: $db_name, type: $db_type}) "
                "MERGE (s)-[:WRITES_TO]->(d)"
            )
            self._run_query(
                query,
                {
                    "service_name": service_name,
                    "repo": self._repo_name,
                    "db_name": db_name,
                    "db_type": db_type,
                },
            )
            logger.debug(
                "WRITES_TO: %s → %s (%s, method: %s)",
                service_name,
                db_name,
                db_type,
                method_name,
            )

        if is_read:
            query = (
                "MERGE (s:Service {name: $service_name, repo: $repo}) "
                "MERGE (d:Database {name: $db_name, type: $db_type}) "
                "MERGE (s)-[:READS_FROM]->(d)"
            )
            self._run_query(
                query,
                {
                    "service_name": service_name,
                    "repo": self._repo_name,
                    "db_name": db_name,
                    "db_type": db_type,
                },
            )
            logger.debug(
                "READS_FROM: %s ← %s (%s, method: %s)",
                service_name,
                db_name,
                db_type,
                method_name,
            )

    def _run_query(self, query: str, parameters: dict) -> None:
        """
        Execute a Cypher write query inside a managed session.

        All Cypher in this module uses MERGE rather than CREATE so writes are
        always idempotent and safe to retry.

        Parameters
        ----------
        query : str
            A Cypher write query string with ``$param`` placeholders.
        parameters : dict
            Parameter values to substitute into the query.
        """
        if self._driver is None:
            # Driver not available — silently skip.  The warning was already
            # logged during __init__.
            return

        try:
            with self._driver.session() as session:
                session.execute_write(lambda tx: tx.run(query, **parameters))
        except Exception as exc:
            logger.warning(
                "Neo4j query failed: %s\n  Query: %.120s\n  Params: %s",
                exc,
                query,
                parameters,
            )


# ---------------------------------------------------------------------------
# Module-level utility
# ---------------------------------------------------------------------------

def _infer_http_method(raw_code: str) -> str:
    """
    Infer the HTTP method (GET/POST/PUT/DELETE) from annotation keywords in code.

    Scans for framework-specific annotation strings and returns the most
    specific match found.  Defaults to ``"GET"`` when nothing matches.

    Parameters
    ----------
    raw_code : str
        Raw source code text to scan.

    Returns
    -------
    str
        One of ``"GET"``, ``"POST"``, ``"PUT"``, ``"DELETE"``.
    """
    # Check from most specific to least specific so DELETE isn't shadowed by GET.
    if any(p in raw_code for p in ["@DeleteMapping", "[HttpDelete]"]):
        return "DELETE"
    if any(p in raw_code for p in ["@PutMapping", "[HttpPut]"]):
        return "PUT"
    if any(p in raw_code for p in ["@PostMapping", "[HttpPost]"]):
        return "POST"
    return "GET"
