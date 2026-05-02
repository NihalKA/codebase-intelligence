"""
indexer/writers/__init__.py
---------------------------
Package init for the indexer writers sub-package.

Writers persist data produced by the parsers into the storage layer:
  - QdrantWriter  → vector embeddings + payload into Qdrant (port 6333)
  - Neo4jWriter   → service relationship graph into Neo4j  (port 7687)

Both writers call only services that run locally (Qdrant, Neo4j, Ollama).
No data leaves the network. See DECISIONS.md → locked-in values for all
port numbers, collection names, and node/relationship types.

Folder convention (DECISIONS.md):
  indexer/parsers/  — language parsers         (read code, produce chunk dicts)
  indexer/clients/  — external service clients (Sourcegraph)
  indexer/writers/  — database writers         ← this package
"""

from indexer.writers.qdrant_writer import QdrantWriter
from indexer.writers.neo4j_writer import Neo4jWriter

__all__ = ["QdrantWriter", "Neo4jWriter"]
