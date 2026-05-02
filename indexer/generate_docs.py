"""
indexer/generate_docs.py — Auto-generate service documentation from Neo4j + Qdrant.

Reads all Service nodes from Neo4j and their relationships (CALLS, PUBLISHES_TO,
CONSUMES_FROM, EXPOSES, READS_FROM, WRITES_TO).  For each service, fetches the
class-level summary from Qdrant.  Outputs:

  1. One Markdown file per service → docs/services/{service-name}.md
  2. One architecture overview    → docs/architecture-overview.md  (Mermaid graph LR)

All generated docs are also uploaded to MinIO (bucket: service-docs).
Optionally pushed to Confluence and/or committed to the repo — both off by default,
activated only by environment variables.

Usage
-----
  python indexer/generate_docs.py
  python indexer/generate_docs.py --output-dir docs/services/ --overview-file docs/architecture-overview.md

Environment variables
---------------------
  NEO4J_URI             bolt://localhost:7687
  NEO4J_USER            neo4j
  NEO4J_PASSWORD        (required — script exits if absent)

  QDRANT_HOST           localhost
  QDRANT_PORT           6333
  QDRANT_API_KEY        (optional)

  MINIO_ENDPOINT        localhost:9000
  MINIO_ACCESS_KEY      (required for MinIO upload)
  MINIO_SECRET_KEY      (required for MinIO upload)
  MINIO_SECURE          false

  CONFLUENCE_URL        (optional — Confluence push skipped if absent)
  CONFLUENCE_TOKEN      (optional — needed alongside CONFLUENCE_URL)
  CONFLUENCE_SPACE_KEY  (optional — needed alongside CONFLUENCE_URL)

  COMMIT_DOCS_TO_REPO   false  (set to "true" to git add/commit/push generated docs)
"""

import argparse
import io
import logging
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

# load_dotenv() must run before any env reads, including those inside
# imported modules that read env vars at import time.
# Path is resolved relative to this file so it works regardless of CWD.
load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent.parent / "infrastructure" / ".env")

from minio import Minio                                        # noqa: E402
from minio.error import S3Error                                # noqa: E402
from neo4j import GraphDatabase                                # noqa: E402
from qdrant_client import QdrantClient                         # noqa: E402
from qdrant_client import models as qdrant_models              # noqa: E402

logger = logging.getLogger("indexer.generate_docs")

# ---------------------------------------------------------------------------
# Constants — collection name and MinIO bucket locked in DECISIONS.md
# ---------------------------------------------------------------------------
_COLLECTION_NAME: str = "codebase-index"
_MINIO_BUCKET: str = "service-docs"


# ═══════════════════════════════════════════════════════════════════════════
# Client builders
# ═══════════════════════════════════════════════════════════════════════════


def _build_neo4j_driver():
    """
    Build a Neo4j driver from environment variables.

    Returns
    -------
    neo4j.Driver or None
        None if NEO4J_PASSWORD is absent or the connection fails.
        Caller must treat None as a fatal condition and exit.
    """
    password: Optional[str] = os.environ.get("NEO4J_PASSWORD")
    if not password:
        logger.warning(
            "NEO4J_PASSWORD not set — cannot connect to Neo4j. "
            "Doc generation requires graph data."
        )
        return None

    uri: str = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user: str = os.environ.get("NEO4J_USER", "neo4j")

    try:
        driver = GraphDatabase.driver(uri, auth=(user, password))
        # verify_connectivity() raises if Neo4j is unreachable.
        driver.verify_connectivity()
        logger.info("Neo4j connected at %s", uri)
        return driver
    except Exception as exc:
        logger.error("Failed to connect to Neo4j at %s: %s", uri, exc)
        return None


def _build_qdrant_client() -> QdrantClient:
    """
    Build a QdrantClient from environment variables.

    Returns
    -------
    QdrantClient
        Configured client.

    Raises
    ------
    Exception
        Propagated to caller so main() can exit cleanly on connection failure.
    """
    host: str = os.environ.get("QDRANT_HOST", "localhost")
    port: int = int(os.environ.get("QDRANT_PORT", "6333"))
    api_key: Optional[str] = os.environ.get("QDRANT_API_KEY") or None
    return QdrantClient(host=host, port=port, api_key=api_key)


def _build_minio_client() -> Minio:
    """
    Build a MinIO client from environment variables.

    Connection failures surface at upload time rather than here, so this
    function never raises — it simply returns a client object.

    Returns
    -------
    Minio
        Configured MinIO client.
    """
    endpoint: str = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
    access_key: str = os.environ.get("MINIO_ACCESS_KEY", "")
    secret_key: str = os.environ.get("MINIO_SECRET_KEY", "")
    secure: bool = os.environ.get("MINIO_SECURE", "false").lower() == "true"

    return Minio(
        endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=secure,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Neo4j queries
# ═══════════════════════════════════════════════════════════════════════════


def _fetch_all_services(driver) -> list[dict]:
    """
    Return all Service nodes from Neo4j, ordered by name.

    Parameters
    ----------
    driver : neo4j.Driver
        Active Neo4j driver.

    Returns
    -------
    list[dict]
        Each dict has keys ``name`` and ``repo``.
        Returns [] on any error — never raises.
    """
    cypher = (
        "MATCH (s:Service) "
        "RETURN s.name AS name, s.repo AS repo "
        "ORDER BY s.name"
    )
    try:
        with driver.session() as session:
            result = session.run(cypher)
            return [{"name": r["name"], "repo": r["repo"]} for r in result]
    except Exception as exc:
        logger.error("Failed to fetch services from Neo4j: %s", exc)
        return []


def _fetch_service_detail(driver, service_name: str) -> dict:
    """
    Fetch all relationships for a single Service node from Neo4j.

    Runs six separate Cypher queries (one per relationship type) so a single
    query failure does not blank out the whole page.

    Parameters
    ----------
    driver : neo4j.Driver
        Active Neo4j driver.
    service_name : str
        The ``name`` property of the Service node to look up.

    Returns
    -------
    dict
        Keys: ``publishes_to``, ``consumes_from``, ``endpoints``,
        ``calls``, ``called_by``, ``databases``.
        Each value is a list (may be empty).
    """
    detail: dict = {
        "publishes_to": [],
        "consumes_from": [],
        "endpoints": [],
        "calls": [],
        "called_by": [],
        "databases": [],
    }

    # Each query keyed by the detail field it fills.
    queries: dict[str, str] = {
        "publishes_to": (
            "MATCH (s:Service {name: $name})-[:PUBLISHES_TO]->(q:Queue) "
            "RETURN q.name AS queue ORDER BY q.name"
        ),
        "consumes_from": (
            "MATCH (s:Service {name: $name})-[:CONSUMES_FROM]->(q:Queue) "
            "RETURN q.name AS queue ORDER BY q.name"
        ),
        "endpoints": (
            "MATCH (s:Service {name: $name})-[:EXPOSES]->(e:Endpoint) "
            "RETURN e.method AS method, e.path AS path "
            "ORDER BY e.method, e.path"
        ),
        "calls": (
            "MATCH (s:Service {name: $name})-[:CALLS]->(t:Service) "
            "RETURN t.name AS name ORDER BY t.name"
        ),
        "called_by": (
            "MATCH (caller:Service)-[:CALLS]->(s:Service {name: $name}) "
            "RETURN caller.name AS name ORDER BY caller.name"
        ),
        "databases": (
            "MATCH (s:Service {name: $name})-[r:READS_FROM|WRITES_TO]->(db:Database) "
            "RETURN type(r) AS rel, db.name AS name, db.type AS type "
            "ORDER BY db.name"
        ),
    }

    try:
        with driver.session() as session:
            for key, cypher in queries.items():
                try:
                    result = session.run(cypher, name=service_name)
                    records = list(result)

                    if key in ("publishes_to", "consumes_from"):
                        detail[key] = [r["queue"] for r in records]
                    elif key == "endpoints":
                        detail[key] = [
                            {"method": r["method"], "path": r["path"]}
                            for r in records
                        ]
                    elif key in ("calls", "called_by"):
                        detail[key] = [r["name"] for r in records]
                    elif key == "databases":
                        detail[key] = [
                            {
                                "rel": r["rel"],
                                "name": r["name"],
                                "type": r["type"],
                            }
                            for r in records
                        ]
                except Exception as exc:
                    logger.warning(
                        "Neo4j query '%s' failed for service '%s': %s",
                        key, service_name, exc
                    )
    except Exception as exc:
        logger.error(
            "Failed to open Neo4j session for service '%s': %s",
            service_name, exc
        )

    return detail


# ═══════════════════════════════════════════════════════════════════════════
# Qdrant queries
# ═══════════════════════════════════════════════════════════════════════════


def _fetch_service_summary(
    qdrant_client: QdrantClient,
    service_name: str,
    repo_name: str,
) -> str:
    """
    Fetch the class-level summary for a service from the Qdrant collection.

    Tries two searches:
      1. Primary — matches both ``repo`` and ``method`` (class name) fields.
      2. Fallback — matches ``repo`` only and returns the first chunk's summary.

    Parameters
    ----------
    qdrant_client : QdrantClient
        Active Qdrant client.
    service_name : str
        Expected to match the ``method`` payload field of a class-level chunk.
    repo_name : str
        Expected to match the ``repo`` payload field.

    Returns
    -------
    str
        Summary text, or "" if nothing is found.
    """
    # Primary attempt — class-level chunk for this exact service name.
    try:
        results, _cursor = qdrant_client.scroll(
            collection_name=_COLLECTION_NAME,
            scroll_filter=qdrant_models.Filter(
                must=[
                    qdrant_models.FieldCondition(
                        key="repo",
                        match=qdrant_models.MatchValue(value=repo_name),
                    ),
                    qdrant_models.FieldCondition(
                        key="method",
                        match=qdrant_models.MatchValue(value=service_name),
                    ),
                ]
            ),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
        if results:
            return results[0].payload.get("summary", "")
    except Exception as exc:
        logger.warning(
            "Qdrant scroll (primary) failed for %s/%s: %s",
            repo_name, service_name, exc
        )

    # Fallback — any chunk from this repo.
    try:
        results, _cursor = qdrant_client.scroll(
            collection_name=_COLLECTION_NAME,
            scroll_filter=qdrant_models.Filter(
                must=[
                    qdrant_models.FieldCondition(
                        key="repo",
                        match=qdrant_models.MatchValue(value=repo_name),
                    ),
                ]
            ),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
        if results:
            return results[0].payload.get("summary", "")
    except Exception as exc:
        logger.warning(
            "Qdrant scroll (fallback) failed for repo '%s': %s", repo_name, exc
        )

    return ""


# ═══════════════════════════════════════════════════════════════════════════
# Markdown rendering
# ═══════════════════════════════════════════════════════════════════════════


def _render_service_page(
    detail: dict,
    summary: str,
    service_name: str,
    repo_name: str,
    generated_at: str,
) -> str:
    """
    Render a single service Markdown page from graph detail and Qdrant summary.

    Parameters
    ----------
    detail : dict
        Output of ``_fetch_service_detail()``.
    summary : str
        Plain-English summary from Qdrant (may be empty).
    service_name : str
        Service name used as the page title.
    repo_name : str
        Repository name displayed under ## Repository.
    generated_at : str
        ISO 8601 UTC timestamp shown in the auto-generated notice.

    Returns
    -------
    str
        Complete Markdown page as a string.
    """
    lines: list[str] = []

    lines.append(f"# {service_name}")
    lines.append("")
    lines.append(
        f"> Auto-generated on {generated_at}. "
        "Do not edit manually — regenerated on every push."
    )
    lines.append("")

    # What this service does
    lines.append("## What this service does")
    lines.append(
        summary
        if summary
        else "_No summary available — service has not been indexed yet._"
    )
    lines.append("")

    # Repository
    lines.append("## Repository")
    lines.append(f"`{repo_name}`")
    lines.append("")

    # API endpoints
    lines.append("## API endpoints")
    if detail["endpoints"]:
        lines.append("| Method | Path |")
        lines.append("|--------|------|")
        for ep in detail["endpoints"]:
            lines.append(f"| {ep['method']} | {ep['path']} |")
    else:
        lines.append("_No endpoints detected._")
    lines.append("")

    # Message queues
    lines.append("## Message queues")
    if detail["publishes_to"]:
        lines.append(
            "**Publishes to:** "
            + ", ".join(f"`{q}`" for q in detail["publishes_to"])
        )
    else:
        lines.append("**Publishes to:** none detected")
    lines.append("")
    if detail["consumes_from"]:
        lines.append(
            "**Consumes from:** "
            + ", ".join(f"`{q}`" for q in detail["consumes_from"])
        )
    else:
        lines.append("**Consumes from:** none detected")
    lines.append("")

    # Calls these services
    lines.append("## Calls these services")
    if detail["calls"]:
        for svc in detail["calls"]:
            lines.append(f"- {svc}")
    else:
        lines.append("_None detected._")
    lines.append("")

    # Called by these services
    lines.append("## Called by these services")
    if detail["called_by"]:
        for svc in detail["called_by"]:
            lines.append(f"- {svc}")
    else:
        lines.append("_None detected._")
    lines.append("")

    # Databases
    lines.append("## Databases")
    if detail["databases"]:
        lines.append("| Name | Type | Direction |")
        lines.append("|------|------|-----------|")
        for db in detail["databases"]:
            direction = "reads" if db["rel"] == "READS_FROM" else "writes"
            lines.append(f"| {db['name']} | {db['type']} | {direction} |")
    else:
        lines.append("_None detected._")
    lines.append("")

    return "\n".join(lines)


def _render_overview(
    all_services: list[dict],
    all_details: dict,
    generated_at: str,
) -> str:
    """
    Render the architecture overview Markdown with a Mermaid ``graph LR`` diagram.

    Only CALLS edges appear in the diagram — queues and endpoints are omitted
    to keep the diagram readable.  Services with no CALLS relationships are
    listed in a separate "Standalone services" section below the diagram.

    Parameters
    ----------
    all_services : list[dict]
        List of ``{name, repo}`` dicts from ``_fetch_all_services()``.
    all_details : dict
        Mapping of service_name → detail dict from ``_fetch_service_detail()``.
    generated_at : str
        ISO 8601 UTC timestamp.

    Returns
    -------
    str
        Complete Markdown page as a string.
    """
    lines: list[str] = []

    lines.append("# Architecture Overview")
    lines.append("")
    lines.append(
        f"> Auto-generated on {generated_at}. "
        "Shows all detected cross-service CALLS relationships."
    )
    lines.append("")

    # Build edge list and track which services appear in the diagram.
    edges: list[str] = []
    connected: set[str] = set()

    for svc in all_services:
        name = svc["name"]
        detail = all_details.get(name, {})
        for target in detail.get("calls", []):
            # Sanitise names: Mermaid node IDs cannot contain spaces or dots.
            src = name.replace(" ", "-").replace(".", "-")
            tgt = target.replace(" ", "-").replace(".", "-")
            edges.append(f"  {src} --> {tgt}")
            connected.add(name)
            connected.add(target)

    if edges:
        lines.append("```mermaid")
        lines.append("graph LR")
        # Deduplicate edges while preserving first-seen order.
        seen: set[str] = set()
        for edge in edges:
            if edge not in seen:
                seen.add(edge)
                lines.append(edge)
        lines.append("```")
    else:
        lines.append("_No cross-service CALLS relationships detected yet._")

    # Services with no CALLS edges in either direction.
    all_names = {s["name"] for s in all_services}
    standalone = sorted(all_names - connected)
    if standalone:
        lines.append("")
        lines.append("## Standalone services")
        lines.append(
            "These services have no detected CALLS relationships "
            "(they may still use queues or databases):"
        )
        lines.append("")
        for name in standalone:
            lines.append(f"- {name}")

    lines.append("")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# Output writers
# ═══════════════════════════════════════════════════════════════════════════


def _write_local(output_dir: str, filename: str, content: str) -> None:
    """
    Write a Markdown file to the local filesystem.

    Creates the output directory if it does not exist.

    Parameters
    ----------
    output_dir : str
        Target directory.
    filename : str
        File name only (no path separators).
    content : str
        File content.
    """
    try:
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, filename)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        logger.debug("Wrote local file: %s", path)
    except Exception as exc:
        logger.error(
            "Failed to write local file %s/%s: %s", output_dir, filename, exc
        )


def _upload_minio(minio_client: Minio, object_name: str, content: str) -> None:
    """
    Upload content to the ``service-docs`` bucket in MinIO.

    Creates the bucket if it does not already exist.
    Upload failures are non-fatal — logged as warnings and execution continues.

    Parameters
    ----------
    minio_client : Minio
        Configured MinIO client (from _build_minio_client).
    object_name : str
        Object key inside the bucket, e.g. ``"services/payment-service.md"``.
    content : str
        Markdown content to upload.
    """
    # Ensure bucket exists — ignore BucketAlreadyOwnedByYou.
    try:
        if not minio_client.bucket_exists(_MINIO_BUCKET):
            minio_client.make_bucket(_MINIO_BUCKET)
            logger.info("Created MinIO bucket: %s", _MINIO_BUCKET)
    except S3Error as exc:
        if "BucketAlreadyOwnedByYou" not in str(exc):
            logger.warning("MinIO bucket check/create failed: %s", exc)
    except Exception as exc:
        logger.warning("MinIO bucket check/create failed: %s", exc)

    try:
        data = content.encode("utf-8")
        minio_client.put_object(
            _MINIO_BUCKET,
            object_name,
            io.BytesIO(data),
            length=len(data),
            content_type="text/markdown",
        )
        logger.debug("Uploaded to MinIO: %s/%s", _MINIO_BUCKET, object_name)
    except Exception as exc:
        logger.warning(
            "MinIO upload failed for %s/%s: %s", _MINIO_BUCKET, object_name, exc
        )


def _push_confluence(page_title: str, content: str) -> None:
    """
    Push a Markdown page to Confluence via its REST API.

    Only called when CONFLUENCE_URL and CONFLUENCE_TOKEN are both set.
    Uses a deferred ``import requests`` so the script runs without the
    ``requests`` package installed when Confluence is not configured.

    Parameters
    ----------
    page_title : str
        Title of the Confluence page to create or update.
    content : str
        Markdown content (sent as wiki storage format).
    """
    confluence_url: Optional[str] = os.environ.get("CONFLUENCE_URL")
    confluence_token: Optional[str] = os.environ.get("CONFLUENCE_TOKEN")
    space_key: Optional[str] = os.environ.get("CONFLUENCE_SPACE_KEY")

    # Guard — caller should check, but be safe.
    if not confluence_url or not confluence_token:
        return

    if not space_key:
        logger.warning(
            "CONFLUENCE_URL and CONFLUENCE_TOKEN are set but "
            "CONFLUENCE_SPACE_KEY is missing — skipping Confluence push for '%s'.",
            page_title,
        )
        return

    try:
        # Deferred import — requests is optional.
        import requests  # noqa: PLC0415

        api_url = f"{confluence_url.rstrip('/')}/wiki/rest/api/content"
        headers = {
            "Authorization": f"Bearer {confluence_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "type": "page",
            "title": page_title,
            "space": {"key": space_key},
            "body": {
                "storage": {
                    "value": content,
                    "representation": "wiki",
                }
            },
        }

        response = requests.post(
            api_url, json=payload, headers=headers, timeout=30
        )
        if response.ok:
            logger.info("Pushed to Confluence: %s", page_title)
        else:
            logger.warning(
                "Confluence push failed for '%s': HTTP %s — %s",
                page_title, response.status_code, response.text[:200],
            )
    except Exception as exc:
        logger.warning("Confluence push failed for '%s': %s", page_title, exc)


def _commit_to_repo(output_dir: str, overview_file: str) -> None:
    """
    Git add, commit, and push the generated docs.

    Only executes when ``COMMIT_DOCS_TO_REPO=true``.
    Uses ``[skip ci]`` in the commit message to prevent triggering
    index-on-push.yml recursively.

    All subprocess calls use argument lists — the shell parameter is never set to True.

    Parameters
    ----------
    output_dir : str
        Directory containing per-service Markdown files.
    overview_file : str
        Path to the architecture overview Markdown file.
    """
    commit_flag: str = os.environ.get("COMMIT_DOCS_TO_REPO", "false")
    if commit_flag.lower() != "true":
        return

    try:
        subprocess.run(
            ["git", "add", output_dir, overview_file],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "git", "commit",
                "-m", "docs: auto-regenerate service pages [skip ci]",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "push"],
            check=True,
            capture_output=True,
            text=True,
        )
        logger.info("Committed and pushed generated docs to repo.")
    except subprocess.CalledProcessError as exc:
        logger.warning(
            "Git commit/push failed (exit %d): %s", exc.returncode, exc.stderr
        )
    except Exception as exc:
        logger.warning("Git commit/push failed: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════


def main() -> None:
    """
    Parse CLI arguments and run the full doc generation pipeline.

    Pipeline steps:
      1. Build Neo4j, Qdrant, and MinIO clients.
      2. Fetch all Service nodes from Neo4j.
      3. For each service: fetch relationships + Qdrant summary → render → write + upload.
      4. Render the architecture overview (Mermaid graph LR) → write + upload.
      5. Optionally push to Confluence and/or commit to repo.

    Exit codes:
      0 — completed (MinIO/Confluence failures are non-fatal)
      1 — fatal: Neo4j or Qdrant unreachable
    """
    parser = argparse.ArgumentParser(
        prog="generate_docs",
        description=(
            "Auto-generate per-service Markdown docs and an architecture overview "
            "from Neo4j and Qdrant."
        ),
    )
    parser.add_argument(
        "--output-dir",
        default="docs/services/",
        help="Local directory for per-service files (default: docs/services/).",
    )
    parser.add_argument(
        "--overview-file",
        default="docs/architecture-overview.md",
        help="Path for the architecture overview (default: docs/architecture-overview.md).",
    )

    args = parser.parse_args()
    output_dir: str = args.output_dir
    overview_file: str = args.overview_file

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    generated_at: str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ------------------------------------------------------------------
    # Build clients — Neo4j and Qdrant are required; MinIO failure is deferred.
    # ------------------------------------------------------------------
    neo4j_driver = _build_neo4j_driver()
    if neo4j_driver is None:
        print(
            "ERROR: Neo4j is required for doc generation. "
            "Set NEO4J_PASSWORD and ensure Neo4j is reachable."
        )
        sys.exit(1)

    try:
        qdrant_client = _build_qdrant_client()
    except Exception as exc:
        logger.error("Failed to build Qdrant client: %s", exc)
        print(f"ERROR: Could not connect to Qdrant — {exc}")
        neo4j_driver.close()
        sys.exit(1)

    minio_client = _build_minio_client()

    # ------------------------------------------------------------------
    # Main generation loop — always close Neo4j driver when done.
    # ------------------------------------------------------------------
    try:
        services = _fetch_all_services(neo4j_driver)
        if not services:
            print(
                "No services found in Neo4j — has any repo been indexed?\n"
                "Run: python indexer/index_repos.py --repo /path/to/repo --name my-service"
            )
            sys.exit(0)

        total = len(services)
        print(f"Found {total} service(s) in Neo4j. Generating docs…")

        all_details: dict = {}

        for idx, svc in enumerate(services, start=1):
            svc_name: str = svc["name"]
            repo_name: str = svc.get("repo") or "unknown-repo"

            # Fetch all graph relationships for this service.
            detail = _fetch_service_detail(neo4j_driver, svc_name)
            all_details[svc_name] = detail

            # Fetch the class-level summary from Qdrant.
            summary = _fetch_service_summary(qdrant_client, svc_name, repo_name)

            # Render Markdown.
            page = _render_service_page(
                detail, summary, svc_name, repo_name, generated_at
            )

            # Write to local filesystem.
            filename = f"{svc_name}.md"
            _write_local(output_dir, filename, page)

            # Upload to MinIO (bucket: service-docs, locked in DECISIONS.md).
            _upload_minio(minio_client, f"services/{filename}", page)

            # Optional: push to Confluence.
            if os.environ.get("CONFLUENCE_URL") and os.environ.get("CONFLUENCE_TOKEN"):
                _push_confluence(svc_name, page)

            print(f"[{idx:>3}/{total}] {svc_name} → {output_dir}{filename}")

        # ------------------------------------------------------------------
        # Architecture overview
        # ------------------------------------------------------------------
        overview_md = _render_overview(services, all_details, generated_at)

        overview_dir = os.path.dirname(overview_file) or "."
        overview_basename = os.path.basename(overview_file)
        _write_local(overview_dir, overview_basename, overview_md)
        _upload_minio(minio_client, "architecture-overview.md", overview_md)

        if os.environ.get("CONFLUENCE_URL") and os.environ.get("CONFLUENCE_TOKEN"):
            _push_confluence("Architecture Overview", overview_md)

        # ------------------------------------------------------------------
        # Optional: commit generated docs to repo.
        # ------------------------------------------------------------------
        if os.environ.get("COMMIT_DOCS_TO_REPO", "false").lower() == "true":
            _commit_to_repo(output_dir, overview_file)

        print(
            f"\nDone. {total} service page(s) + architecture overview generated."
        )

    finally:
        # Always close the Neo4j driver, even if an exception occurred.
        try:
            neo4j_driver.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
