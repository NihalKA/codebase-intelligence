"""
indexer/clients/__init__.py
---------------------------
Package init for the indexer clients sub-package.

Clients wrap external service APIs used during indexing.  They are kept
separate from parsers and writers so each concern stays in its own layer
(see DECISIONS.md → Indexer clients folder = indexer/clients/).

Public exports:
  SourcegraphClient — optional GraphQL client for cross-repo symbol search.
                       Gracefully degrades to empty results when Sourcegraph
                       is unreachable; never raises.
"""

from indexer.clients.sourcegraph_client import SourcegraphClient

__all__ = ["SourcegraphClient"]
