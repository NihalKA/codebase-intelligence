"""
index_diff.py — Phase 3 / incremental indexing entry point.

Indexes only the files that changed since a given git ref (or an explicit
list of files), instead of the whole repository.  Used by the CI workflow
(Phase 4) so each push re-indexes only the modified source files.

Flow
----
  1. Resolve the list of changed .java / .cs files
     - from --files (explicit list), or
     - from git diff --name-only <ref>
  2. For each changed file:
     a. Delete existing Qdrant points for that file (payload filter on "file")
     b. Parse the file (tree-sitter)
     c. Write chunks to Qdrant    (Steps 3–4 of index_repos.py)
     d. Write relationships to Neo4j
  3. Sourcegraph cross-repo lookup for service names found (same as
     index_repos.py Step 5)

Usage
-----
  # Explicit file list:
  python indexer/index_diff.py --files src/PaymentService.java --name payment-service

  # Auto-detect from git diff against a ref:
  python indexer/index_diff.py --git-diff HEAD~1 --name payment-service

  # Specify a non-current repo root when running from elsewhere:
  python indexer/index_diff.py --git-diff HEAD~1 --name payment-service --repo /path/to/repo

All connection details come from environment variables — QDRANT_HOST,
QDRANT_PORT, QDRANT_API_KEY, OLLAMA_HOST, OLLAMA_MODEL, NEO4J_URI,
NEO4J_USER, NEO4J_PASSWORD, SOURCEGRAPH_URL, SOURCEGRAPH_TOKEN.
"""

import argparse
import logging
import os
import pathlib
import subprocess
import sys
import time
from typing import Optional

from dotenv import load_dotenv

# load_dotenv() must run before any module that reads env vars at import time.
# Path is resolved relative to this file so it works regardless of CWD.
load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent.parent / "infrastructure" / ".env")

from indexer.clients.sourcegraph_client import SourcegraphClient  # noqa: E402
from indexer.parsers.dotnet_parser import parse_dotnet_file       # noqa: E402
from indexer.parsers.java_parser import parse_java_file           # noqa: E402
from indexer.writers.neo4j_writer import Neo4jWriter              # noqa: E402
from indexer.writers.qdrant_writer import QdrantWriter            # noqa: E402

# QdrantClient is needed directly for the delete operation — QdrantWriter
# does not expose a delete method so we manage our own client for deletes.
from qdrant_client import QdrantClient                            # noqa: E402
from qdrant_client import models as qdrant_models                 # noqa: E402

logger = logging.getLogger("indexer.index_diff")

# ---------------------------------------------------------------------------
# Constants — collection name locked in DECISIONS.md
# ---------------------------------------------------------------------------
_COLLECTION_NAME: str = "codebase-index"

# ---------------------------------------------------------------------------
# Skip rules — identical to index_repos.py
# ---------------------------------------------------------------------------
_SKIP_FILENAME_SUFFIXES: tuple[str, ...] = ("Test.java", "Tests.cs")


def _should_skip(file_path: str) -> bool:
    """
    Return True if the file should be excluded from indexing.

    Same rules as index_repos.py: test dirs, generated output dirs,
    and files named *Test.java / *Tests.cs.

    Parameters
    ----------
    file_path : str
        Path to the source file.

    Returns
    -------
    bool
        True if the file should be skipped.
    """
    normalized = file_path.replace("\\", "/")
    for fragment in ("/test/", "/obj/", "/target/", "/bin/"):
        if fragment in normalized:
            return True
    basename = os.path.basename(file_path)
    return basename.endswith("Test.java") or basename.endswith("Tests.cs")


def _resolve_files_from_git(repo_root: str, git_ref: str) -> list[str]:
    """
    Run ``git diff --name-only <ref>`` and return changed .java/.cs paths.

    Parameters
    ----------
    repo_root : str
        Absolute path to the git repository root.
    git_ref : str
        Git ref to diff against, e.g. ``"HEAD~1"`` or a commit SHA.

    Returns
    -------
    list[str]
        Repo-relative paths of changed .java and .cs files.

    Raises
    ------
    SystemExit
        If git is not available, the path is not a repo, or the ref is invalid.
    """
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, "diff", "--name-only", git_ref],
            capture_output=True,
            text=True,
            check=True,  # raises CalledProcessError on non-zero exit
        )
    except FileNotFoundError:
        logger.error("'git' executable not found — cannot use --git-diff.")
        print("ERROR: 'git' not found. Install git or use --files instead.")
        sys.exit(1)
    except subprocess.CalledProcessError as exc:
        logger.error(
            "git diff failed for ref '%s': %s\n%s",
            git_ref, exc, exc.stderr
        )
        print(f"ERROR: git diff --name-only {git_ref} failed: {exc.stderr.strip()}")
        sys.exit(1)

    changed: list[str] = []
    for line in result.stdout.splitlines():
        path = line.strip()
        if not path:
            continue
        if not (path.endswith(".java") or path.endswith(".cs")):
            continue
        # git returns repo-relative paths — make them absolute for open()
        # but keep relative for Qdrant payload "file" field.
        if not os.path.isabs(path):
            abs_path = os.path.join(repo_root, path)
        else:
            abs_path = path
        if os.path.isfile(abs_path):
            changed.append(path)   # store repo-relative
        else:
            logger.debug("Changed file no longer exists, skipping: %s", path)

    return changed


def _build_qdrant_client() -> QdrantClient:
    """
    Build a QdrantClient from environment variables for delete operations.

    Returns
    -------
    QdrantClient
        Configured client instance.
    """
    host: str = os.environ.get("QDRANT_HOST", "localhost")
    port: int = int(os.environ.get("QDRANT_PORT", "6333"))
    api_key: Optional[str] = os.environ.get("QDRANT_API_KEY") or None
    return QdrantClient(host=host, port=port, api_key=api_key)


def _delete_file_points(qdrant_client: QdrantClient, rel_file_path: str) -> None:
    """
    Delete all Qdrant points whose ``file`` payload field matches the given path.

    Called before re-indexing a changed file so outdated vectors are removed
    before the new ones are inserted.

    Parameters
    ----------
    qdrant_client : QdrantClient
        Qdrant client instance (built by _build_qdrant_client).
    rel_file_path : str
        Repo-relative file path exactly as stored in the Qdrant ``file``
        payload field, e.g. ``"src/main/PaymentService.java"``.
    """
    try:
        qdrant_client.delete(
            collection_name=_COLLECTION_NAME,
            points_selector=qdrant_models.FilterSelector(
                filter=qdrant_models.Filter(
                    must=[
                        qdrant_models.FieldCondition(
                            key="file",
                            match=qdrant_models.MatchValue(value=rel_file_path),
                        )
                    ]
                )
            ),
        )
        logger.debug("Deleted old Qdrant points for: %s", rel_file_path)
    except Exception as exc:
        # Non-fatal — if delete fails, write_chunk's deterministic point IDs
        # will overwrite stale points anyway.
        logger.warning(
            "Could not delete old Qdrant points for %s: %s", rel_file_path, exc
        )


def _parse_file(file_path: str) -> list[dict]:
    """
    Parse a source file and return its chunks.

    Parameters
    ----------
    file_path : str
        Absolute path to the .java or .cs file.

    Returns
    -------
    list[dict]
        Chunk dicts, or empty list on error.
    """
    if file_path.endswith(".java"):
        return parse_java_file(file_path)
    if file_path.endswith(".cs"):
        return parse_dotnet_file(file_path)
    logger.warning("Unrecognised extension, skipping: %s", file_path)
    return []


def _run_diff_indexing(
    repo_root: str,
    repo_name: str,
    rel_file_paths: list[str],
) -> int:
    """
    Execute the incremental indexing pipeline for a list of changed files.

    Parameters
    ----------
    repo_root : str
        Absolute path to the repository root.
    repo_name : str
        Short canonical name for the repo.
    rel_file_paths : list[str]
        Repo-relative paths of changed files to re-index.

    Returns
    -------
    int
        Exit code: 0 on success (errors are non-fatal), 1 for fatal setup.
    """
    t_start = time.time()
    total_files = len(rel_file_paths)
    print(f"Incremental index: {repo_name} — {total_files} changed file(s)")

    if total_files == 0:
        print("No indexable changed files found.")
        return 0

    # ------------------------------------------------------------------
    # Instantiate writers once — reused across all changed files
    # ------------------------------------------------------------------
    try:
        qdrant_java = QdrantWriter(repo_name, "java")
        qdrant_cs   = QdrantWriter(repo_name, "csharp")
        qdrant_del  = _build_qdrant_client()  # separate client for deletes
    except Exception as exc:
        logger.error("Failed to initialise QdrantWriter: %s", exc)
        print(f"ERROR: Could not connect to Qdrant — {exc}")
        return 1

    neo4j_writer = Neo4jWriter(repo_name)
    sg_client = SourcegraphClient()

    service_names: set[str] = {repo_name}
    files_indexed = 0
    chunks_created = 0
    errors = 0

    try:
        for idx, rel_path in enumerate(rel_file_paths, start=1):
            abs_path = os.path.join(repo_root, rel_path)

            if _should_skip(rel_path):
                logger.debug("Skipping (excluded): %s", rel_path)
                continue

            if not os.path.isfile(abs_path):
                logger.warning("Changed file not found on disk: %s", abs_path)
                errors += 1
                continue

            # Step 2a — delete stale Qdrant points for this file
            print(f"Deleting old points for: {rel_path}")
            _delete_file_points(qdrant_del, rel_path)

            # Step 2b — parse
            try:
                chunks = _parse_file(abs_path)
            except Exception as exc:
                logger.error("Parse error in %s: %s", rel_path, exc)
                errors += 1
                print(f"[{idx:>3}/{total_files}] {rel_path} ... ERROR (parse): {exc}")
                continue

            # Rewrite chunk file paths to repo-relative form.
            for chunk in chunks:
                chunk["file"] = rel_path

            # Collect class names for Step 5 Sourcegraph lookup.
            for chunk in chunks:
                if chunk.get("type") == "class_declaration":
                    service_names.add(chunk.get("name", ""))

            chunk_count = 0
            qdrant_writer = qdrant_java if rel_path.endswith(".java") else qdrant_cs

            # Step 2c — write new chunks to Qdrant
            for chunk in chunks:
                try:
                    success = qdrant_writer.write_chunk(chunk)
                    if success:
                        chunk_count += 1
                    else:
                        errors += 1
                except Exception as exc:
                    logger.error(
                        "Qdrant write error for chunk '%s' in %s: %s",
                        chunk.get("name"), rel_path, exc
                    )
                    errors += 1

            # Step 2d — write graph relationships to Neo4j
            neo4j_writer.ensure_service(repo_name)
            for chunk in chunks:
                try:
                    neo4j_writer.write_relationships(chunk, repo_name)
                except Exception as exc:
                    logger.error(
                        "Neo4j write error for chunk '%s' in %s: %s",
                        chunk.get("name"), rel_path, exc
                    )
                    errors += 1

            files_indexed += 1
            chunks_created += chunk_count
            print(f"[{idx:>3}/{total_files}] {rel_path} ... {chunk_count} chunks")

        # ------------------------------------------------------------------
        # Step 3 — Sourcegraph cross-repo CALLS lookup
        # ------------------------------------------------------------------
        service_names.discard("")
        if sg_client.is_available():
            print(
                f"\nStep 3 — Sourcegraph cross-repo lookup: "
                f"{len(service_names)} service name(s) queried"
            )
            for svc_name in sorted(service_names):
                try:
                    caller_results = sg_client.search_callers(svc_name, svc_name)
                    for result in caller_results:
                        caller_repo = result.get("repo", "unknown-repo")
                        if caller_repo != repo_name:
                            neo4j_writer.write_cross_repo_call(
                                source_service=caller_repo,
                                target_service=svc_name,
                                source_repo=caller_repo,
                                target_repo=repo_name,
                            )
                except Exception as exc:
                    logger.warning(
                        "Sourcegraph lookup failed for %s: %s", svc_name, exc
                    )
        else:
            print(
                "\nStep 3 — Sourcegraph unavailable or not configured, "
                "cross-repo lookup skipped."
            )

    finally:
        neo4j_writer.close()

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    elapsed = time.time() - t_start
    divider = "─" * 52
    print(f"\n{divider}")
    print(
        f"Done. {files_indexed} files, {chunks_created} chunks, "
        f"{errors} errors  ({elapsed:.1f}s)"
    )
    return 0


def main() -> None:
    """
    Parse CLI arguments and run the incremental indexing pipeline.

    --files and --git-diff are mutually exclusive.  At least one must be
    provided.

    Exit codes:
      0 — completed
      1 — fatal error (bad args, git failure, Qdrant unreachable)
    """
    parser = argparse.ArgumentParser(
        prog="index_diff",
        description=(
            "Incrementally re-index only the files that changed since a git ref, "
            "or an explicit list of files."
        ),
    )
    parser.add_argument(
        "--name",
        required=True,
        help="Short canonical repo name (e.g. 'payment-service').",
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="Path to the repository root (default: current directory).",
    )

    # --files and --git-diff are mutually exclusive source-of-truth for what
    # changed; exactly one must be provided.
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "--files",
        nargs="+",
        metavar="FILE",
        help="Explicit list of changed file paths to re-index.",
    )
    source_group.add_argument(
        "--git-diff",
        metavar="GIT_REF",
        help="Git ref to diff against, e.g. HEAD~1 or a commit SHA.",
    )

    args = parser.parse_args()

    repo_root = os.path.abspath(args.repo)
    if not os.path.isdir(repo_root):
        print(f"ERROR: --repo path does not exist: {repo_root}")
        sys.exit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # Resolve the list of changed files.
    if args.files:
        # Normalise to repo-relative paths; accept both absolute and relative.
        rel_files: list[str] = []
        for f in args.files:
            abs_f = os.path.abspath(f) if not os.path.isabs(f) else f
            try:
                rel = os.path.relpath(abs_f, repo_root)
            except ValueError:
                rel = f  # cross-drive fallback on Windows
            if rel.endswith(".java") or rel.endswith(".cs"):
                rel_files.append(rel)
            else:
                logger.debug("Ignoring non-Java/CS file: %s", f)
    else:
        rel_files = _resolve_files_from_git(repo_root, args.git_diff)

    exit_code = _run_diff_indexing(repo_root, args.name, rel_files)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
