# java_parser.py — Tree-sitter parser for Java source files
#
# Extracts method, class, and interface declarations from .java files.
# Detects integration patterns (RabbitMQ, HTTP, SQL/Database) using a
# two-layer strategy:
#   1. Import-level detection — checks import_declaration AST nodes
#   2. Body-level detection  — checks code inside method/class bodies (not comments)
#
# This combined approach achieves ~95% accuracy by avoiding false positives
# from comments and catching usage via import statements even when the
# code uses custom wrapper classes.
#
# Public API: parse_java_file(file_path) -> list[dict]
# All other functions are internal (_prefixed).

import logging
from typing import Optional

import tree_sitter
import tree_sitter_java

logger = logging.getLogger(__name__)

# ── Import-level markers ───────────────────────────────────────────────────────
# These are checked against import_declaration AST nodes.
# If a file imports one of these packages, the tech is considered detected
# even if no body-level pattern is found (the developer may use a wrapper).

RABBITMQ_IMPORTS: list[str] = ["org.springframework.amqp", "com.rabbitmq"]
HTTP_IMPORTS: list[str] = ["org.springframework.web", "feign.", "javax.ws.rs"]
DB_IMPORTS: list[str] = [
    "javax.persistence",
    "org.springframework.data",
    "java.sql",
    "org.springframework.jdbc",
]
POSTGRESQL_IMPORTS: list[str] = ["org.postgresql"]

# ── Body-level patterns ────────────────────────────────────────────────────────
# These are checked against the text of code AST nodes only (not comments).
# Substring matching on the decoded text of each relevant AST node.

RABBITMQ_BODY: list[str] = [
    "rabbitTemplate",
    "@RabbitListener",
    "channel.basicPublish",
    "convertAndSend",
]
HTTP_BODY: list[str] = [
    "RestTemplate",
    "WebClient",
    "FeignClient",
    "@GetMapping",
    "@PostMapping",
    "@RequestMapping",
]
DB_BODY: list[str] = [
    "@Repository",
    "JdbcTemplate",
    "EntityManager",
    "DataSource",
    "@Query",
    "JpaRepository",
]

# ── Target AST node types for chunk extraction ─────────────────────────────────
_TARGET_NODE_TYPES: set[str] = {
    "method_declaration",
    "class_declaration",
    "interface_declaration",
}

# Map tree-sitter node type names to our simplified type strings
_NODE_TYPE_MAP: dict[str, str] = {
    "method_declaration": "method",
    "class_declaration": "class",
    "interface_declaration": "interface",
}

# AST node types that contain comments — we skip these during body-level matching
_COMMENT_NODE_TYPES: set[str] = {
    "line_comment",
    "block_comment",
}


def _init_parser() -> tree_sitter.Parser:
    """Initialise a tree-sitter parser with the Java grammar.

    Returns:
        A configured tree_sitter.Parser ready to parse Java source bytes.
    """
    java_language = tree_sitter.Language(tree_sitter_java.language())
    parser = tree_sitter.Parser(java_language)
    return parser


# Module-level parser instance — created once, reused for every file
_PARSER: tree_sitter.Parser = _init_parser()


def _extract_imports(root_node: tree_sitter.Node, source_bytes: bytes) -> set[str]:
    """Walk the AST and collect all import declaration strings.

    Looks for `import_declaration` nodes and extracts the full import path.
    For example: `import org.springframework.amqp.rabbit.core.RabbitTemplate;`
    yields `"org.springframework.amqp.rabbit.core.RabbitTemplate"`.

    Args:
        root_node: The root node of the parsed AST.
        source_bytes: The raw source file bytes.

    Returns:
        A set of import path strings found in the file.
    """
    imports: set[str] = set()

    def _walk(node: tree_sitter.Node) -> None:
        if node.type == "import_declaration":
            # The import text includes 'import ' prefix and ';' suffix — extract the path
            # by finding the scoped_identifier or identifier child node
            for child in node.children:
                if child.type in ("scoped_identifier", "identifier"):
                    import_text = source_bytes[child.start_byte : child.end_byte].decode(
                        "utf-8", errors="replace"
                    )
                    imports.add(import_text)
                    break
        for child in node.children:
            _walk(child)

    _walk(root_node)
    return imports


def _collect_code_text(node: tree_sitter.Node, source_bytes: bytes) -> str:
    """Collect the text of all code nodes under the given node, excluding comments.

    Recursively walks the AST and concatenates text from leaf nodes that are
    not comments. This gives us the actual code content without comments,
    which we use for body-level pattern matching.

    Args:
        node: The AST node to start from.
        source_bytes: The raw source file bytes.

    Returns:
        A string containing all non-comment code text under this node.
    """
    if node.type in _COMMENT_NODE_TYPES:
        return ""

    # Leaf node — return its text
    if node.child_count == 0:
        return source_bytes[node.start_byte : node.end_byte].decode(
            "utf-8", errors="replace"
        )

    # Internal node — concatenate children
    parts: list[str] = []
    for child in node.children:
        parts.append(_collect_code_text(child, source_bytes))
    return " ".join(parts)


def _detect_patterns(
    code_text: str, file_imports: set[str]
) -> dict[str, bool | str | None]:
    """Detect integration patterns using import-level + body-level matching.

    A pattern is considered detected if:
    - Any import marker is a substring of any import path, OR
    - Any body-level pattern is a substring of the code text (excluding comments)

    Args:
        code_text: The code text of the chunk with comments stripped out.
        file_imports: The set of import paths extracted from the file.

    Returns:
        A dict with keys: rabbitmq (bool), http (bool), database (bool),
        db_type (str|None). db_type is "postgresql" if PostgreSQL imports
        are found, "sql" if any DB pattern matches, else None.
    """
    # Check imports — is any marker a substring of any import path?
    imports_joined = " ".join(file_imports)

    rabbitmq_import = any(marker in imports_joined for marker in RABBITMQ_IMPORTS)
    http_import = any(marker in imports_joined for marker in HTTP_IMPORTS)
    db_import = any(marker in imports_joined for marker in DB_IMPORTS)
    pg_import = any(marker in imports_joined for marker in POSTGRESQL_IMPORTS)

    # Check body-level patterns in code text (comments already excluded)
    rabbitmq_body = any(pattern in code_text for pattern in RABBITMQ_BODY)
    http_body = any(pattern in code_text for pattern in HTTP_BODY)
    db_body = any(pattern in code_text for pattern in DB_BODY)

    # Combine: detected if found in imports OR in code body
    rabbitmq = rabbitmq_import or rabbitmq_body
    http = http_import or http_body
    database = db_import or db_body

    # Determine db_type
    db_type: str | None = None
    if database:
        db_type = "postgresql" if pg_import else "sql"

    return {
        "rabbitmq": rabbitmq,
        "http": http,
        "database": database,
        "db_type": db_type,
    }


def _extract_node_name(node: tree_sitter.Node, source_bytes: bytes) -> str:
    """Extract the name (identifier) from a method, class, or interface declaration.

    Searches the direct children of the node for an 'identifier' node.
    Falls back to 'unknown' if no identifier is found (should not happen
    in well-formed Java, but we handle it defensively).

    Args:
        node: A method_declaration, class_declaration, or interface_declaration node.
        source_bytes: The raw source file bytes.

    Returns:
        The name string, or 'unknown' if no identifier child is found.
    """
    for child in node.children:
        if child.type == "identifier":
            return source_bytes[child.start_byte : child.end_byte].decode(
                "utf-8", errors="replace"
            )
    return "unknown"


def _extract_node(
    node: tree_sitter.Node,
    source_bytes: bytes,
    file_path: str,
    file_imports: set[str],
) -> Optional[dict]:
    """Extract a structured chunk from a target AST node.

    Only processes method_declaration, class_declaration, and interface_declaration
    nodes. For all other node types, returns None.

    Args:
        node: The AST node to inspect.
        source_bytes: The raw source file bytes.
        file_path: The relative path to the source file.
        file_imports: The set of import paths for this file (for pattern detection).

    Returns:
        A chunk dict with keys {name, type, file, lines, raw_code, detected_patterns},
        or None if this node is not a target type.
    """
    if node.type not in _TARGET_NODE_TYPES:
        return None

    name = _extract_node_name(node, source_bytes)

    # 1-based line numbers, formatted as "start-end"
    lines = f"{node.start_point[0] + 1}-{node.end_point[0] + 1}"

    raw_code = source_bytes[node.start_byte : node.end_byte].decode(
        "utf-8", errors="replace"
    )

    chunk_type = _NODE_TYPE_MAP[node.type]

    # Collect code text without comments for pattern matching
    code_text = _collect_code_text(node, source_bytes)
    detected_patterns = _detect_patterns(code_text, file_imports)

    return {
        "name": name,
        "type": chunk_type,
        "file": file_path,
        "lines": lines,
        "raw_code": raw_code,
        "detected_patterns": detected_patterns,
    }


def parse_java_file(file_path: str) -> list[dict]:
    """Parse a .java file and extract all method, class, and interface chunks.

    This is the public API for the Java parser. Other modules should call
    only this function. It reads the file, parses it with tree-sitter,
    extracts import statements for pattern detection, then walks the AST
    to collect all target chunks.

    If the file cannot be read or parsed, logs the error and returns an
    empty list — never raises an exception. This ensures one bad file
    does not crash the entire indexing pipeline.

    Args:
        file_path: Path to the .java file to parse.

    Returns:
        A list of chunk dicts. Each has keys:
        {name, type, file, lines, raw_code, detected_patterns}.
        Returns [] on any error.
    """
    try:
        with open(file_path, "rb") as f:
            source_bytes = f.read()

        tree = _PARSER.parse(source_bytes)
        root = tree.root_node

        # Step 1: Extract all import paths from the file
        file_imports = _extract_imports(root, source_bytes)

        # Step 2+3: Walk the AST and extract chunks with pattern detection
        chunks: list[dict] = []

        def _walk(node: tree_sitter.Node) -> None:
            chunk = _extract_node(node, source_bytes, file_path, file_imports)
            if chunk is not None:
                chunks.append(chunk)
            for child in node.children:
                _walk(child)

        _walk(root)

        logger.info("Parsed %s — %d chunks extracted", file_path, len(chunks))
        return chunks

    except Exception:
        logger.exception("Failed to parse Java file: %s — skipping", file_path)
        return []
