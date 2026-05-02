"""
indexer/writers/qdrant_writer.py
---------------------------------
Writes parser chunk dicts into Qdrant as vector embeddings with metadata.

For each chunk the writer:
  1. Calls local Ollama to generate a plain-English summary (generate endpoint).
  2. Calls local Ollama to produce a vector embedding (embeddings endpoint).
  3. Upserts the point into the `codebase-index` Qdrant collection with the
     exact payload fields required by DECISIONS.md:
       repo, file, method, lines, language, summary, indexed_at

Collection is created on first use with Cosine distance.  Vector dimension is
determined at runtime by probing the embedding model output.

Configuration (all from environment — never hardcoded):
  QDRANT_HOST      Qdrant hostname        (default: localhost)
  QDRANT_PORT      Qdrant HTTP port       (default: 6333)
  QDRANT_API_KEY   Optional API key       (default: "" — none required on LAN)
  OLLAMA_HOST      Ollama base URL        (default: http://localhost:11434)
  OLLAMA_MODEL     Model for summarise    (default: deepseek-coder:6.7b)

All Qdrant and Ollama calls are wrapped in try/except.  A failed upsert logs a
warning and returns False — it never crashes the indexing run.
"""

import logging
import os
import pathlib
import uuid
from datetime import datetime, timezone
from typing import Optional

import ollama as ollama_lib
from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.http import models as qdrant_models
from qdrant_client.http.exceptions import UnexpectedResponse

# ---------------------------------------------------------------------------
# Logging — pipeline logs at INFO so each file's progress is visible in CI.
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# Path is resolved relative to this file so it works regardless of CWD.
load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent.parent.parent / "infrastructure" / ".env")

# ---------------------------------------------------------------------------
# Collection name is locked in DECISIONS.md — must not be changed here.
# ---------------------------------------------------------------------------
_COLLECTION_NAME: str = "codebase-index"

# Namespace UUID used to derive deterministic point IDs (uuid5).
# Using NAMESPACE_URL as a stable, well-known base.
_UUID_NAMESPACE: uuid.UUID = uuid.NAMESPACE_URL

# Maximum characters of raw_code sent to Ollama for summarisation.
# Keeps prompt size manageable without losing the structure of the chunk.
_MAX_CODE_CHARS: int = 2000

# Prompt template for chunk summarisation.
_SUMMARY_PROMPT = (
    "Summarise this {chunk_type} named '{chunk_name}' in one paragraph of "
    "plain English. Focus on what it does, not how it is implemented.\n\n"
    "{code_snippet}"
)


class QdrantWriter:
    """
    Persists parser chunk dicts into Qdrant as searchable vector embeddings.

    One instance is created per indexing run.  It holds open connections to
    both Qdrant and Ollama for the duration of the run so TCP connections
    are reused across all chunks.

    Parameters
    ----------
    repo_name : str
        Short name of the repository being indexed, e.g. ``"payment-service"``.
        Written into every point's ``repo`` payload field.
    language : str
        Either ``"java"`` or ``"csharp"``.  Written into every point's
        ``language`` payload field.
    """

    def __init__(self, repo_name: str, language: str) -> None:
        self._repo_name: str = repo_name
        self._language: str = language

        # -- Qdrant connection details from environment ----------------------
        qdrant_host: str = os.environ.get("QDRANT_HOST", "localhost")
        qdrant_port: int = int(os.environ.get("QDRANT_PORT", "6333"))
        qdrant_api_key: Optional[str] = os.environ.get("QDRANT_API_KEY") or None

        # -- Ollama connection details from environment ----------------------
        self._ollama_host: str = os.environ.get(
            "OLLAMA_HOST", "http://localhost:11434"
        )
        self._model: str = os.environ.get("OLLAMA_MODEL", "deepseek-coder:6.7b")

        # -- Create Qdrant client --------------------------------------------
        # https= must be set explicitly: QdrantClient enables TLS automatically
        # when api_key is provided, which breaks plain-HTTP local deployments.
        # Only use HTTPS when an API key is actually configured.
        qdrant_https: bool = qdrant_api_key is not None
        try:
            self._qdrant = QdrantClient(
                host=qdrant_host,
                port=qdrant_port,
                api_key=qdrant_api_key,
                https=qdrant_https,
                prefer_grpc=False,
            )
            logger.info(
                "Qdrant client connected to %s:%d", qdrant_host, qdrant_port
            )
        except Exception as exc:
            logger.error(
                "Failed to connect to Qdrant at %s:%d: %s",
                qdrant_host,
                qdrant_port,
                exc,
            )
            raise

        # -- Create Ollama client --------------------------------------------
        try:
            self._ollama = ollama_lib.Client(host=self._ollama_host)
            logger.info(
                "Ollama client pointed at %s (model: %s)",
                self._ollama_host,
                self._model,
            )
        except Exception as exc:
            logger.error(
                "Failed to create Ollama client for host %s: %s",
                self._ollama_host,
                exc,
            )
            raise

        # -- Ensure the Qdrant collection exists with the right schema -------
        self._ensure_collection()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write_chunk(self, chunk: dict) -> bool:
        """
        Summarise, embed, and upsert one parser chunk into Qdrant.

        Builds the canonical DECISIONS.md payload:
          repo, file, method, lines, language, summary, indexed_at

        The point ID is deterministic (uuid5 from repo + file + name + lines)
        so re-indexing the same chunk updates the existing point in-place.

        Parameters
        ----------
        chunk : dict
            A chunk dict produced by ``parse_java_file`` or
            ``parse_dotnet_file``.  Expected keys:
            ``name``, ``type``, ``file``, ``lines``, ``raw_code``.

        Returns
        -------
        bool
            ``True`` if the point was upserted successfully, ``False``
            if any step failed (the caller should count failures but not stop).
        """
        chunk_name: str = chunk.get("name", "unknown")
        chunk_type: str = chunk.get("type", "chunk")
        file_path: str = chunk.get("file", "")

        # Step 1 — Generate a plain-English summary via Ollama generate API.
        summary: str = self._summarise(chunk)

        # Step 2 — Embed (name + summary) for semantic search quality.
        # Embedding the name alongside the summary keeps method-name lookups
        # accurate while the summary text adds semantic richness.
        embedding_text: str = f"{chunk_name} {summary}"
        vector: list[float] = self._embed(embedding_text)

        if not vector:
            logger.warning(
                "Skipping upsert for '%s' in %s — embedding returned empty.",
                chunk_name,
                file_path,
            )
            return False

        # Step 3 — Deterministic point ID using uuid5 so re-runs overwrite
        # the same point rather than creating duplicates.
        point_id_str: str = (
            f"{self._repo_name}:{file_path}:{chunk_name}:{chunk.get('lines', '')}"
        )
        point_id: str = str(uuid.uuid5(_UUID_NAMESPACE, point_id_str))

        # Build the payload with exactly the 7 fields required by DECISIONS.md.
        payload: dict = {
            "repo": self._repo_name,
            "file": file_path,
            "method": chunk_name,
            "lines": chunk.get("lines", ""),
            "language": self._language,
            "summary": summary,
            "indexed_at": datetime.now(timezone.utc).isoformat(),
        }

        # Step 4 — Upsert to Qdrant.  Errors are logged and swallowed so the
        # indexer can continue with remaining chunks.
        try:
            self._qdrant.upsert(
                collection_name=_COLLECTION_NAME,
                points=[
                    qdrant_models.PointStruct(
                        id=point_id,
                        vector=vector,
                        payload=payload,
                    )
                ],
            )
            logger.debug(
                "Upserted '%s' (%s) → Qdrant point %s", chunk_name, file_path, point_id
            )
            return True

        except Exception as exc:
            logger.warning(
                "Qdrant upsert failed for '%s' in %s: %s",
                chunk_name,
                file_path,
                exc,
            )
            return False

    def delete_by_file(self, file_path: str) -> int:
        """
        Delete all Qdrant points whose ``file`` payload field matches the given path.

        Called by ``index_diff.py`` before re-indexing a changed file so old
        points are removed and not left alongside the new ones.

        Parameters
        ----------
        file_path : str
            The repo-relative file path to purge, e.g. ``"src/PaymentService.java"``.

        Returns
        -------
        int
            Number of points deleted, or ``0`` if the operation failed.
        """
        try:
            result = self._qdrant.delete(
                collection_name=_COLLECTION_NAME,
                points_selector=qdrant_models.FilterSelector(
                    filter=qdrant_models.Filter(
                        must=[
                            qdrant_models.FieldCondition(
                                key="file",
                                match=qdrant_models.MatchValue(value=file_path),
                            )
                        ]
                    )
                ),
            )
            # The delete result does not expose a count; log the operation.
            logger.info("Deleted Qdrant points for file: %s (result: %s)", file_path, result)
            return 1  # Signal success; exact count not available from this API.

        except Exception as exc:
            logger.warning(
                "Failed to delete Qdrant points for file %s: %s", file_path, exc
            )
            return 0

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _ensure_collection(self) -> None:
        """
        Create the ``codebase-index`` Qdrant collection if it does not exist.

        Checks for the collection first; creates it with Cosine distance and
        the correct vector dimension only when absent.  Safe to call on every
        startup because it is a no-op when the collection already exists.

        Raises on fatal errors (e.g. Ollama unreachable when we need the
        vector dimension) so the indexer surfaces the problem early rather
        than failing silently on every individual upsert.
        """
        try:
            self._qdrant.get_collection(_COLLECTION_NAME)
            logger.info("Qdrant collection '%s' already exists.", _COLLECTION_NAME)
            return
        except (UnexpectedResponse, Exception) as exc:
            # Collection does not yet exist — check the error message to confirm
            # before attempting to create it.
            if "not found" in str(exc).lower() or "doesn't exist" in str(exc).lower() or "404" in str(exc):
                logger.info(
                    "Qdrant collection '%s' not found — creating it.", _COLLECTION_NAME
                )
            else:
                # Some other error (e.g. Qdrant unreachable) — re-raise so the
                # indexer fails fast with a clear message.
                logger.error(
                    "Unexpected error checking Qdrant collection '%s': %s",
                    _COLLECTION_NAME,
                    exc,
                )
                raise

        # Determine the embedding dimension by probing the model with a short text.
        # This is done once at startup — the result determines the collection schema.
        dimension: int = self._get_embedding_dimension()

        try:
            self._qdrant.create_collection(
                collection_name=_COLLECTION_NAME,
                vectors_config=qdrant_models.VectorParams(
                    size=dimension,
                    distance=qdrant_models.Distance.COSINE,
                ),
            )
            logger.info(
                "Created Qdrant collection '%s' (dim=%d, distance=Cosine).",
                _COLLECTION_NAME,
                dimension,
            )
        except Exception as exc:
            logger.error(
                "Failed to create Qdrant collection '%s': %s", _COLLECTION_NAME, exc
            )
            raise

    def _get_embedding_dimension(self) -> int:
        """
        Probe the Ollama embedding model to determine its output vector size.

        Used once during ``_ensure_collection`` to set the correct ``size``
        when creating the collection.  Raises if Ollama is unreachable — the
        indexer cannot proceed without knowing the vector dimension.

        Returns
        -------
        int
            Number of dimensions in the embedding vector (e.g. 4096 for
            deepseek-coder:6.7b).
        """
        try:
            response = self._ollama.embeddings(model=self._model, prompt="probe")
            dimension = len(response["embedding"])
            logger.info(
                "Ollama model '%s' produces %d-dimensional embeddings.",
                self._model,
                dimension,
            )
            return dimension
        except Exception as exc:
            logger.error(
                "Failed to probe embedding dimension from Ollama "
                "model '%s' at %s: %s",
                self._model,
                self._ollama_host,
                exc,
            )
            raise

    def _summarise(self, chunk: dict) -> str:
        """
        Generate a plain-English summary of a chunk using Ollama generate API.

        Truncates ``raw_code`` to ``_MAX_CODE_CHARS`` characters to keep
        prompts manageable.  Returns ``"Summary unavailable"`` if Ollama
        fails — this is preferable to letting one bad chunk stop the run.

        Parameters
        ----------
        chunk : dict
            Parser chunk dict with keys ``name``, ``type``, ``raw_code``.

        Returns
        -------
        str
            A short paragraph describing what the chunk does.
        """
        raw_code: str = chunk.get("raw_code", "")
        code_snippet: str = raw_code[:_MAX_CODE_CHARS]

        prompt: str = _SUMMARY_PROMPT.format(
            chunk_type=chunk.get("type", "code block"),
            chunk_name=chunk.get("name", "unnamed"),
            code_snippet=code_snippet,
        )

        try:
            response = self._ollama.generate(model=self._model, prompt=prompt)
            summary: str = response["response"].strip()
            logger.debug(
                "Summarised '%s': %d chars.", chunk.get("name", "?"), len(summary)
            )
            return summary
        except Exception as exc:
            logger.warning(
                "Ollama summary failed for '%s': %s — using placeholder.",
                chunk.get("name", "unknown"),
                exc,
            )
            return "Summary unavailable."

    def _embed(self, text: str) -> list[float]:
        """
        Produce an embedding vector for a text string using Ollama.

        Parameters
        ----------
        text : str
            The text to embed.  Typically ``"<method_name> <summary>"``.

        Returns
        -------
        list[float]
            The embedding vector, or an empty list ``[]`` if Ollama fails.
            The caller checks for an empty list and skips the upsert.
        """
        try:
            response = self._ollama.embeddings(model=self._model, prompt=text)
            return response["embedding"]
        except Exception as exc:
            logger.warning("Ollama embedding failed for text '%.40s…': %s", text, exc)
            return []
