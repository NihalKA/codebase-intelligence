"""
index_repos.py — Phase 3 / full-repo indexing entry point.

Walks every .java and .cs file in a cloned repository, parses each file
with tree-sitter, summarises and embeds every chunk via Ollama, stores
chunks + embeddings in Qdrant, writes service relationship graphs to Neo4j,
and finally queries Sourcegraph for cross-repo caller relationships.

Five-step flow
--------------
  Step 1  Walk the repo tree, collect .java / .cs files (skip tests+generated)
  Step 2  Parse each file with java_parser or dotnet_parser (tree-sitter)
  Step 3  Write every chunk to Qdrant (summarise → embed → upsert)
  Step 4  Write graph relationships to Neo4j for every chunk
  Step 5  Query Sourcegraph for cross-repo CALLS edges (optional, non-fatal)

Usage
-----
  # From repo root:
  python indexer/index_repos.py --repo /path/to/repo --name payment-service
  python indexer/index_repos.py --repo /path/to/repo --name payment-service --language java

  # As module:
  python -m indexer.index_repos --repo /path/to/repo --name payment-service

All connection details come from environment variables — see the imports of
QdrantWriter, Neo4jWriter, and SourcegraphClient for the full list.
"""

import argparse
import logging
import os
import pathlib
import sys
import time

from dotenv import load_dotenv

# load_dotenv() must run before any module that reads env vars at import time
# (e.g. writers that instantiate clients in __init__).
# Path is resolved relative to this file so it works regardless of CWD.
load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent.parent / "infrastructure" / ".env")

from indexer.clients.sourcegraph_client import SourcegraphClient  # noqa: E402
from indexer.parsers.dotnet_parser import parse_dotnet_file       # noqa: E402
from indexer.parsers.java_parser import parse_java_file           # noqa: E402
from indexer.writers.neo4j_writer import Neo4jWriter              # noqa: E402
from indexer.writers.qdrant_writer import QdrantWriter            # noqa: E402

logger = logging.getLogger("indexer.index_repos")

# ---------------------------------------------------------------------------
# Skip rules (from PHASE2-indexer.md)
# ---------------------------------------------------------------------------

# Path segment fragments that indicate test directories or generated output.
# Using os.sep-independent patterns: check both / and \ delimiters.
_SKIP_PATH_FRAGMENTS: tuple[str, ...] = (
    "/test/", "\\test\\",
    "/obj/",  "\\obj\\",
    "/target/", "\\target\\",
    "/bin/",  "\\bin\\",
)

# Filename suffixes that indicate test files.
_SKIP_FILENAME_SUFFIXES: tuple[str, ...] = (
    "Test.java",
    "Tests.cs",
)


def _should_skip(file_path: str) -> bool:
    """
    Return True if the file should be excluded from indexing.

    Exclusion rules (from PHASE2-indexer.md):
      - Path contains /test/, /obj/, /target/, or /bin/ directory segments
      - Filename ends with Test.java or Tests.cs

    Parameters
    ----------
    file_path : str
        Absolute or relative path to the source file.

    Returns
    -------
    bool
        True if the file should be skipped.
    """
    # Normalize to forward-slash so rules work cross-platform.
    normalized = file_path.replace("\\", "/")
    for fragment in ("/test/", "/obj/", "/target/", "/bin/"):
        if fragment in normalized:
            return True

    basename = os.path.basename(file_path)
    return basename.endswith("Test.java") or basename.endswith("Tests.cs")


def _collect_files(repo_root: str, language: str) -> list[str]:
    """
    Walk the repo tree and return paths of indexable source files.

    Parameters
    ----------
    repo_root : str
        Absolute path to the repository root.
    language : str
        One of "java", "csharp", or "auto".
        "auto" collects both .java and .cs files.

    Returns
    -------
    list[str]
        Absolute file paths that pass the skip filter, in walk order.
    """
    extensions: set[str] = set()
    if language in ("java", "auto"):
        extensions.add(".java")
    if language in ("csharp", "auto"):
        extensions.add(".cs")

    collected: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(repo_root):
        for filename in filenames:
            if not any(filename.endswith(ext) for ext in extensions):
                continue
            full_path = os.path.join(dirpath, filename)
            if _should_skip(full_path):
                logger.debug("Skipping (excluded): %s", full_path)
                continue
            collected.append(full_path)

    return collected


def _detect_language(repo_root: str) -> str:
    """
    Detect the predominant language by counting .java and .cs files.

    Returns "auto" (both) if the counts are roughly balanced, otherwise
    "java" or "csharp" for the dominant language.  In practice returns
    "auto" so both parsers run — the caller decides which parser to use
    per file based on extension.

    Parameters
    ----------
    repo_root : str
        Absolute path to the repository root.

    Returns
    -------
    str
        "java", "csharp", or "auto".
    """
    java_count = 0
    cs_count = 0
    for dirpath, _dirnames, filenames in os.walk(repo_root):
        for filename in filenames:
            if filename.endswith(".java"):
                java_count += 1
            elif filename.endswith(".cs"):
                cs_count += 1

    if java_count == 0 and cs_count == 0:
        logger.warning("No .java or .cs files found under %s", repo_root)
        return "auto"
    if java_count == 0:
        return "csharp"
    if cs_count == 0:
        return "java"
    return "auto"  # both exist — run both parsers


def _parse_file(file_path: str) -> list[dict]:
    """
    Parse a single source file and return its chunks.

    Dispatches to java_parser or dotnet_parser based on file extension.

    Parameters
    ----------
    file_path : str
        Absolute path to the .java or .cs file.

    Returns
    -------
    list[dict]
        Chunk dicts from the parser, or an empty list on parse error.
    """
    if file_path.endswith(".java"):
        return parse_java_file(file_path)
    if file_path.endswith(".cs"):
        return parse_dotnet_file(file_path)
    logger.warning("Unrecognised extension, skipping: %s", file_path)
    return []


def _run_indexing(repo_root: str, repo_name: str, language: str) -> int:
    """
    Execute the full five-step indexing run.

    Parameters
    ----------
    repo_root : str
        Absolute path to the repository root.
    repo_name : str
        Short canonical name for the repo, e.g. "payment-service".
    language : str
        "java", "csharp", or "auto".

    Returns
    -------
    int
        Exit code: 0 on success, 1 if any fatal setup error occurred.
    """
    t_start = time.time()
    print(f"Indexing repo: {repo_name} (language: {language})")

    # ------------------------------------------------------------------
    # Resolve actual language if auto-detection requested
    # ------------------------------------------------------------------
    if language == "auto":
        language = _detect_language(repo_root)
        print(f"Auto-detected language(s): {language}")

    # ------------------------------------------------------------------
    # Step 1 — collect files
    # ------------------------------------------------------------------
    files = _collect_files(repo_root, language)
    total_files = len(files)
    if total_files == 0:
        print("No indexable files found. Check --repo path and --language flag.")
        return 1

    print(f"Found {total_files} file(s) to index.")

    # ------------------------------------------------------------------
    # Instantiate writers once — reused across all files
    # ------------------------------------------------------------------
    # QdrantWriter needs a language string; for mixed repos we create one
    # writer per language and use them based on file extension.
    try:
        qdrant_java = QdrantWriter(repo_name, "java")   if any(f.endswith(".java") for f in files) else None
        qdrant_cs   = QdrantWriter(repo_name, "csharp") if any(f.endswith(".cs")   for f in files) else None
    except Exception as exc:
        logger.error("Failed to initialise QdrantWriter: %s", exc)
        print(f"ERROR: Could not connect to Qdrant — {exc}")
        return 1

    neo4j_writer = Neo4jWriter(repo_name)
    sg_client = SourcegraphClient()

    # Track service class names seen during indexing for Step 5.
    service_names: set[str] = {repo_name}

    files_indexed = 0
    chunks_created = 0
    errors = 0

    try:
        # ------------------------------------------------------------------
        # Steps 2–4 — parse → write Qdrant → write Neo4j, per file
        # ------------------------------------------------------------------
        for idx, file_path in enumerate(files, start=1):
            # Make path relative to repo root for display and payload storage.
            try:
                rel_path = os.path.relpath(file_path, repo_root)
            except ValueError:
                rel_path = file_path  # fallback on Windows cross-drive paths

            try:
                # Step 2 — parse
                chunks = _parse_file(file_path)
            except Exception as exc:
                logger.error("Parse error in %s: %s", rel_path, exc)
                errors += 1
                print(f"[{idx:>4}/{total_files}] {rel_path} ... ERROR (parse): {exc}")
                continue

            # Rewrite chunk file paths to repo-relative form so Qdrant
            # payload "file" fields are consistent across all runs.
            for chunk in chunks:
                chunk["file"] = rel_path

            # Collect class names for Step 5 Sourcegraph lookup.
            for chunk in chunks:
                if chunk.get("type") == "class_declaration":
                    service_names.add(chunk.get("name", ""))

            chunk_count = 0

            # Step 3 — write to Qdrant
            qdrant_writer = qdrant_java if file_path.endswith(".java") else qdrant_cs
            if qdrant_writer is not None:
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

            # Step 4 — write relationships to Neo4j
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
            print(f"[{idx:>4}/{total_files}] {rel_path} ... {chunk_count} chunks")

        # ------------------------------------------------------------------
        # Step 5 — Sourcegraph cross-repo CALLS lookup
        # ------------------------------------------------------------------
        service_names.discard("")  # remove any blank entries
        if sg_client.is_available():
            print(
                f"\nStep 5 — Sourcegraph cross-repo lookup: "
                f"{len(service_names)} service name(s) queried"
            )
            for svc_name in sorted(service_names):
                try:
                    caller_results = sg_client.search_callers(svc_name, svc_name)
                    for result in caller_results:
                        # result dict keys: repo, file, symbol (from SourcegraphClient)
                        caller_repo = result.get("repo", "unknown-repo")
                        if caller_repo != repo_name:
                            # Only record genuine cross-repo calls.
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
                "\nStep 5 — Sourcegraph unavailable or not configured, "
                "cross-repo lookup skipped."
            )

    finally:
        # Always close the Neo4j driver, even if an exception interrupted the run.
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
    return 0 if errors == 0 else 0  # errors are non-fatal, exit 0 always


def main() -> None:
    """
    Parse CLI arguments and run the full-repo indexing pipeline.

    Exit codes:
      0 — completed (errors during individual file writes are non-fatal)
      1 — fatal setup error (bad repo path, Qdrant unreachable)
    """
    parser = argparse.ArgumentParser(
        prog="index_repos",
        description="Index all Java/.NET source files in a repository into Qdrant and Neo4j.",
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="Absolute or relative path to the cloned repository root.",
    )
    parser.add_argument(
        "--name",
        required=True,
        help="Short canonical name for this repo (e.g. 'payment-service').",
    )
    parser.add_argument(
        "--language",
        default="auto",
        choices=["auto", "java", "csharp"],
        help="Source language to index. 'auto' detects from file extensions (default).",
    )

    args = parser.parse_args()

    # Validate repo path exists.
    repo_root = os.path.abspath(args.repo)
    if not os.path.isdir(repo_root):
        print(f"ERROR: --repo path does not exist or is not a directory: {repo_root}")
        sys.exit(1)

    # Configure basic logging so INFO-level messages from writers are visible
    # when running from the command line.
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    exit_code = _run_indexing(repo_root, args.name, args.language)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
