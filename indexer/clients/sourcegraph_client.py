"""
indexer/clients/sourcegraph_client.py
--------------------------------------
Optional Sourcegraph GraphQL client used in the final step of the indexing
pipeline (index_repos.py Step 5) to discover cross-repo callers of a service.

Sourcegraph is explicitly optional for this platform.  If the server is
unreachable, every public method logs a WARNING and returns an empty list.
The indexer must never crash because Sourcegraph is down.

Configuration (environment variables only — never hardcoded):
  SOURCEGRAPH_URL   Base URL, e.g. http://localhost:7080  (from DECISIONS.md)
  SOURCEGRAPH_TOKEN Personal access token (sgp_…)

Connection semantics:
  - All requests use the /.api/graphql endpoint.
  - Auth header: "Authorization: token <SOURCEGRAPH_TOKEN>"
  - connect_timeout=5s, read_timeout=30s on every HTTP call.
  - If the server is unreachable or returns a non-200 response the method
    logs a warning and returns [].  It does NOT raise.

Public API:
  SourcegraphClient.search_symbol(symbol_name)  → list[SymbolMatch]
  SourcegraphClient.search_callers(service, method) → list[SymbolMatch]
  SourcegraphClient.is_available()               → bool

SymbolMatch dict shape:
  {
    "repo":    str,   # e.g. "github.com/company/payment-service"
    "file":    str,   # repo-relative path, e.g. "src/PaymentService.java"
    "line":    int,   # 1-based line number of the match
    "preview": str,   # the matching source line, stripped
  }
"""

import logging
import os
import pathlib
from typing import Any

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Module-level logger.  The indexer pipeline configures the root logger so
# WARNING/ERROR messages appear in the console even without extra setup.
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# Load infrastructure/.env so local development works without manually exporting vars.
# Path is resolved relative to this file so it works regardless of CWD.
load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent.parent.parent / "infrastructure" / ".env")

# ---------------------------------------------------------------------------
# Timeouts (seconds).  Keep connect short so an absent Sourcegraph does not
# stall the indexer for 30+ seconds per file.
# ---------------------------------------------------------------------------
_CONNECT_TIMEOUT: int = 5
_READ_TIMEOUT: int = 30

# ---------------------------------------------------------------------------
# GraphQL query templates used by this client.
# We use Sourcegraph's standard search API — these queries work on both the
# OSS edition (6.x) and enterprise editions.
# ---------------------------------------------------------------------------

# Generic code-search query.  The caller builds the Sourcegraph query string
# and this template wraps it in a GraphQL envelope.
_GRAPHQL_SEARCH = """
query SearchCode($query: String!) {
  search(query: $query, version: V2) {
    results {
      results {
        __typename
        ... on FileMatch {
          repository {
            name
          }
          file {
            path
          }
          lineMatches {
            lineNumber
            preview
          }
        }
      }
    }
  }
}
"""


class SourcegraphClient:
    """
    Thin wrapper around the Sourcegraph GraphQL API.

    All public methods return a (possibly empty) list and log a warning on
    failure — they never raise so the indexing pipeline continues uninterrupted
    even when Sourcegraph is down.

    Parameters
    ----------
    url : str | None
        Override SOURCEGRAPH_URL env var (useful in tests).
    token : str | None
        Override SOURCEGRAPH_TOKEN env var (useful in tests).
    """

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
    ) -> None:
        # Read connection details from environment — never fall back to a
        # hardcoded URL so there is no risk of accidentally hitting an
        # external server.
        self._base_url: str = (
            url or os.environ.get("SOURCEGRAPH_URL", "")
        ).rstrip("/")
        self._token: str = token or os.environ.get("SOURCEGRAPH_TOKEN", "")

        if not self._base_url:
            logger.warning(
                "SOURCEGRAPH_URL is not set — Sourcegraph client is disabled. "
                "Cross-repo caller lookup will return empty results."
            )

        # Build a reusable session so TCP connections are pooled.
        self._session = requests.Session()
        if self._token:
            # Token is injected once into the session header; every request
            # that uses this session will carry it automatically.
            self._session.headers.update(
                {"Authorization": f"token {self._token}"}
            )

        self._graphql_url: str = f"{self._base_url}/.api/graphql"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """
        Probe Sourcegraph to confirm it is reachable.

        Returns
        -------
        bool
            True if the server responds with HTTP 200, False otherwise.
            Never raises.
        """
        if not self._base_url:
            return False

        try:
            # Use a lightweight introspection query to test connectivity
            # without triggering a real search (avoids index load).
            resp = self._session.post(
                self._graphql_url,
                json={
                    "query": "{ currentUser { username } }",
                    "variables": {},
                },
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            )
            return resp.status_code == 200
        except requests.exceptions.RequestException as exc:
            # ConnectionError, Timeout, etc. — all indicate Sourcegraph is down.
            logger.warning(
                "Sourcegraph availability check failed: %s — "
                "cross-repo lookup will be skipped.",
                exc,
            )
            return False

    def search_symbol(self, symbol_name: str) -> list[dict[str, Any]]:
        """
        Find all files across all repos that reference a symbol.

        Runs the Sourcegraph query:
          ``<symbol_name> type:symbol``

        This matches class names, interface names, and method names indexed
        by Sourcegraph's symbol indexer.

        Parameters
        ----------
        symbol_name : str
            The symbol to search for, e.g. ``"PaymentService"``.

        Returns
        -------
        list[dict]
            Zero or more SymbolMatch dicts (see module docstring).
            Returns [] if Sourcegraph is unreachable or returns an error.
        """
        if not self._base_url:
            logger.warning(
                "search_symbol('%s') skipped — SOURCEGRAPH_URL not configured.",
                symbol_name,
            )
            return []

        # Build a precise symbol search query.
        # "type:symbol" restricts to indexed symbol definitions/references,
        # giving fewer false positives than a plain text search.
        sg_query = f"{symbol_name} type:symbol"
        logger.debug("Sourcegraph symbol search: %r", sg_query)

        return self._run_search(sg_query)

    def search_callers(
        self, service_name: str, method_name: str
    ) -> list[dict[str, Any]]:
        """
        Find cross-repo call sites for a specific service method.

        Runs a Sourcegraph query that looks for the method name appearing
        inside any file that also references the service name.  This is a
        best-effort heuristic — it catches the most common patterns (direct
        method calls, Spring @Autowired injection, .NET dependency injection).

        Parameters
        ----------
        service_name : str
            Owning service / class name, e.g. ``"PaymentService"``.
        method_name : str
            Method being called, e.g. ``"processPayment"``.

        Returns
        -------
        list[dict]
            Zero or more SymbolMatch dicts.
            Returns [] if Sourcegraph is unreachable or returns an error.
        """
        if not self._base_url:
            logger.warning(
                "search_callers('%s', '%s') skipped — SOURCEGRAPH_URL not configured.",
                service_name,
                method_name,
            )
            return []

        # Content search restricted to the exact method-call pattern.
        # This works for both Java (service.method()) and C# (service.Method()).
        sg_query = f"{service_name}.{method_name} type:file"
        logger.debug("Sourcegraph caller search: %r", sg_query)

        return self._run_search(sg_query)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _run_search(self, sg_query: str) -> list[dict[str, Any]]:
        """
        Execute a Sourcegraph GraphQL search and parse results.

        Parameters
        ----------
        sg_query : str
            A fully-formed Sourcegraph search query string.

        Returns
        -------
        list[dict]
            Parsed SymbolMatch dicts extracted from FileMatch results.
            Returns [] on any error — never raises.
        """
        try:
            resp = self._session.post(
                self._graphql_url,
                json={
                    "query": _GRAPHQL_SEARCH,
                    "variables": {"query": sg_query},
                },
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            )
        except requests.exceptions.ConnectionError as exc:
            # Server is down or DNS does not resolve — expected in environments
            # where Sourcegraph has not been deployed yet.
            logger.warning(
                "Sourcegraph is unreachable (%s). "
                "Skipping cross-repo lookup for query: %r",
                exc,
                sg_query,
            )
            return []
        except requests.exceptions.Timeout:
            logger.warning(
                "Sourcegraph request timed out (connect=%ds, read=%ds) "
                "for query: %r — skipping.",
                _CONNECT_TIMEOUT,
                _READ_TIMEOUT,
                sg_query,
            )
            return []
        except requests.exceptions.RequestException as exc:
            # Catch-all for SSL errors, too-many-redirects, etc.
            logger.warning(
                "Sourcegraph request failed: %s — skipping query: %r",
                exc,
                sg_query,
            )
            return []

        if resp.status_code != 200:
            logger.warning(
                "Sourcegraph returned HTTP %d for query %r — skipping.",
                resp.status_code,
                sg_query,
            )
            return []

        # Decode JSON response — protect against malformed payloads.
        try:
            body = resp.json()
        except ValueError as exc:
            logger.warning(
                "Sourcegraph response could not be decoded as JSON: %s", exc
            )
            return []

        # Surface any GraphQL-level errors (schema mismatch, bad query, etc.)
        if "errors" in body:
            logger.warning(
                "Sourcegraph GraphQL errors for query %r: %s",
                sg_query,
                body["errors"],
            )
            # Partial results may still be present in body["data"]; fall
            # through and return whatever we can parse rather than failing hard.

        return self._parse_results(body)

    def _parse_results(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Extract a flat list of SymbolMatch dicts from a raw GraphQL response.

        The GraphQL schema nests results as:
          data → search → results → results → [FileMatch]
          FileMatch → repository.name, file.path, lineMatches[{lineNumber, preview}]

        Each (file, line) pair produces one SymbolMatch dict.

        Parameters
        ----------
        body : dict
            Decoded JSON response from the Sourcegraph /.api/graphql endpoint.

        Returns
        -------
        list[dict]
            Flat list of SymbolMatch dicts; empty list on any navigation error.
        """
        matches: list[dict[str, Any]] = []

        try:
            raw_results = (
                body.get("data", {})
                .get("search", {})
                .get("results", {})
                .get("results", [])
            )
        except AttributeError:
            # body["data"] is None when all queries error out on the server.
            logger.warning(
                "Unexpected Sourcegraph response structure — "
                "data.search.results missing."
            )
            return []

        for item in raw_results:
            # We only care about FileMatch results; skip CommitMatch etc.
            if item.get("__typename") != "FileMatch":
                continue

            repo = item.get("repository", {}).get("name", "")
            file_path = item.get("file", {}).get("path", "")

            for line_match in item.get("lineMatches", []):
                # Sourcegraph lineNumber is 0-based; convert to 1-based for
                # consistency with how every other tool in this platform reports
                # line numbers (tree-sitter, editors, GitHub).
                line_number: int = line_match.get("lineNumber", 0) + 1
                preview: str = line_match.get("preview", "").strip()

                matches.append(
                    {
                        "repo": repo,
                        "file": file_path,
                        "line": line_number,
                        "preview": preview,
                    }
                )

        logger.debug(
            "Sourcegraph parsed %d match(es) from response.", len(matches)
        )
        return matches
