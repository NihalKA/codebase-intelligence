# indexer/parsers/__init__.py
# Public API for the parser package — import these in index_repos.py and index_diff.py

from indexer.parsers.java_parser import parse_java_file
from indexer.parsers.dotnet_parser import parse_dotnet_file

__all__ = ["parse_java_file", "parse_dotnet_file"]
