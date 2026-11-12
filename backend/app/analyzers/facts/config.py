"""Configuration read facts: environment variables and configuration keys.

Only the *name* of a variable or key is recorded. Default values, fallbacks and anything read
from the environment are never stored. Names must be string literals; computed names are skipped.
A cheap byte-marker check skips the second parse for files that cannot contain a read.
"""

import re

from app.analyzers.facts.common import clip, text
from app.analyzers.positions import SourceIndex
from app.analyzers.registry import parse_bytes
from app.contracts.ast_facts import ConfigFactV1, FileFactsV1
from app.contracts.structure import AnalysisBudget, SourceUnitV1
from tree_sitter import Node

MAX_CONFIG = 1024
JS_FAMILY = {"javascript", "jsx", "typescript", "tsx"}
STRING_NODES = {
    "string",
    "string_literal",
    "interpreted_string_literal",
    "raw_string_literal",
    "line_string_literal",
    "encapsed_string",
}
MARKERS: dict[str, tuple[bytes, ...]] = {
    "python": (b"environ", b"getenv", b".config", b"settings"),
    "go": (b"Getenv", b"LookupEnv", b"viper"),
    "rust": (b"env",),
    "ruby": (b"ENV",),
    "php": (b"getenv", b"env(", b"$_ENV", b"config("),
    "swift": (b"environment",),
    "c": (b"getenv",),
    "cpp": (b"getenv",),
    "java": (b"getenv", b"getProperty"),
    **dict.fromkeys(JS_FAMILY, (b"process.env", b"import.meta.env", b"Deno.env", b"config.")),
}
PY_ENV_CALLS = {
    "os.getenv",
    "getenv",
    "os.environ.get",
    "os.environ.setdefault",
    "os.environ.pop",
    "environ.get",
    "environ.pop",
}
PY_ENV_MAPPINGS = {"os.environ", "environ"}
GO_ENV_CALLS = {"os.Getenv", "os.LookupEnv", "syscall.Getenv"}
RUST_ENV_CALLS = {"std::env::var", "env::var", "std::env::var_os", "env::var_os"}
C_ENV_CALLS = {"getenv", "std::getenv", "::getenv", "secure_getenv"}
JS_ENV_OBJECTS = {"process.env", "import.meta.env"}
SPRING_VALUE = re.compile(r"\$\{([^}:]+)")


def _literal(data: bytes, node: Node | None) -> str | None:
    """Content of a plain string literal (possibly wrapped by an argument node), else None."""
    while node is not None and node.type in {"argument", "value_argument"} and node.named_children:
        node = node.child_by_field_name("value") or node.named_children[0]
    if node is None or node.type not in STRING_NODES:
        return None
    parts = [
        text(data, c)
        for c in node.named_children
        if "content" in c.type or "fragment" in c.type or c.type == "line_str_text"
    ]
    if parts:
        return "".join(parts)
    raw = text(data, node)
    return raw.strip("\"'`") if len(raw) >= 2 else None


def _first_arg(node: Node | None) -> Node | None:
    if node is None:
        return None
    return node.named_children[0] if node.named_children else None


def _heads(facts: FileFactsV1) -> set[str]:
    heads = set()
    for item in facts.imports:
        heads.add(item.module.split(".")[0].split("/")[-1])
        heads.add(item.module)
    return heads


def observe(unit: SourceUnitV1, facts: FileFactsV1, budget: AnalysisBudget) -> FileFactsV1:
    language = unit.language
    found: dict[tuple[str, str, int], ConfigFactV1] = {}

    def record(kind: str, name: str | None, node: Node, index: SourceIndex) -> None:
        if not name or len(found) >= MAX_CONFIG:
            return
        key = (kind, name, node.start_byte)
        if key not in found:
            found[key] = ConfigFactV1(
                kind=kind,  # type: ignore[arg-type]
                name=clip(name, 512),
                range=index.range_from_bytes(node.start_byte, node.end_byte),
            )

    # Spring property placeholders live on annotations already captured as decorators.
    if language == "java":
        index = SourceIndex(unit.data)
        for symbol in facts.symbols:
            for decorator in symbol.decorators:
                head = decorator.split("(", 1)[0].strip()
                if head == "Value":
                    for name in SPRING_VALUE.findall(decorator):
                        found[("config_key", name, symbol.range.start_byte)] = ConfigFactV1(
                            kind="config_key", name=clip(name, 512), range=symbol.range
                        )
                elif head == "ConfigurationProperties":
                    prefix = re.search(
                        r"(?:prefix|value)\s*=\s*\"([^\"]+)\"|\(\s*\"([^\"]+)\"", decorator
                    )
                    if prefix:
                        name = prefix.group(1) or prefix.group(2)
                        found[("config_key", name, symbol.range.start_byte)] = ConfigFactV1(
                            kind="config_key", name=clip(name, 512), range=symbol.range
                        )
        del index

    markers = MARKERS.get(language, ())
    if markers and any(marker in unit.data for marker in markers):
        heads = _heads(facts)
        data = unit.data
        index = SourceIndex(data)
        root = parse_bytes(language, data).root_node
        flask = "flask" in heads
        django = "django" in heads
        viper = any("viper" in h for h in heads)
        node_config = "config" in heads
        stack: list[Node] = [root]
        while stack:
            node = stack.pop()
            budget.tick_nodes(1)
            stack.extend(reversed(node.children))
            kind = node.type
            if language == "python":
                if kind == "call":
                    callee = text(data, node.child_by_field_name("function"))
                    arg = _literal(data, _first_arg(node.child_by_field_name("arguments")))
                    if callee in PY_ENV_CALLS:
                        record("env_var", arg, node, index)
                    elif flask and callee.endswith("config.get"):
                        record("config_key", arg, node, index)
                elif kind == "subscript":
                    base = text(data, node.child_by_field_name("value"))
                    key = _literal(data, node.child_by_field_name("subscript"))
                    if base in PY_ENV_MAPPINGS:
                        record("env_var", key, node, index)
                    elif flask and base.endswith("config"):
                        record("config_key", key, node, index)
                elif (
                    django
                    and kind == "attribute"
                    and text(data, node.child_by_field_name("object")) == "settings"
                ):
                    record(
                        "config_key",
                        text(data, node.child_by_field_name("attribute")),
                        node,
                        index,
                    )
            elif language in JS_FAMILY:
                if kind == "member_expression":
                    if text(data, node.child_by_field_name("object")) in JS_ENV_OBJECTS:
                        record(
                            "env_var", text(data, node.child_by_field_name("property")), node, index
                        )
                elif kind == "subscript_expression":
                    if text(data, node.child_by_field_name("object")) in JS_ENV_OBJECTS:
                        record(
                            "env_var",
                            _literal(data, node.child_by_field_name("index")),
                            node,
                            index,
                        )
                elif kind == "call_expression":
                    callee = text(data, node.child_by_field_name("function"))
                    arg = _literal(data, _first_arg(node.child_by_field_name("arguments")))
                    if callee == "Deno.env.get":
                        record("env_var", arg, node, index)
                    elif node_config and callee in {"config.get", "config.has"}:
                        record("config_key", arg, node, index)
            elif language == "go":
                if kind == "call_expression":
                    callee = text(data, node.child_by_field_name("function"))
                    arg = _literal(data, _first_arg(node.child_by_field_name("arguments")))
                    if callee in GO_ENV_CALLS:
                        record("env_var", arg, node, index)
                    elif viper and callee.startswith("viper.Get"):
                        record("config_key", arg, node, index)
            elif language == "java":
                if kind == "method_invocation":
                    receiver = text(data, node.child_by_field_name("object"))
                    method = text(data, node.child_by_field_name("name"))
                    arg = _literal(data, _first_arg(node.child_by_field_name("arguments")))
                    if receiver == "System" and method == "getenv":
                        record("env_var", arg, node, index)
                    elif method == "getProperty" and receiver in {"System", "env", "environment"}:
                        record("config_key", arg, node, index)
            elif language == "rust":
                if kind == "call_expression":
                    callee = text(data, node.child_by_field_name("function"))
                    if callee in RUST_ENV_CALLS:
                        record(
                            "env_var",
                            _literal(data, _first_arg(node.child_by_field_name("arguments"))),
                            node,
                            index,
                        )
                elif kind == "macro_invocation" and text(
                    data, node.child_by_field_name("macro")
                ) in {"env", "option_env"}:
                    tree_node = next(
                        (c for c in node.named_children if c.type == "token_tree"), None
                    )
                    record("env_var", _literal(data, _first_arg(tree_node)), node, index)
            elif language == "ruby":
                if (
                    kind == "element_reference"
                    and text(data, node.child_by_field_name("object")) == "ENV"
                ):
                    record("env_var", _literal(data, _first_arg_after_object(node)), node, index)
                elif (
                    kind == "call"
                    and text(data, node.child_by_field_name("receiver")) == "ENV"
                    and text(data, node.child_by_field_name("method")) in {"fetch", "dig", "key?"}
                ):
                    record(
                        "env_var",
                        _literal(data, _first_arg(node.child_by_field_name("arguments"))),
                        node,
                        index,
                    )
            elif language == "php":
                if kind == "function_call_expression":
                    callee = text(data, node.child_by_field_name("function"))
                    arg = _literal(data, _first_arg(node.child_by_field_name("arguments")))
                    if callee in {"getenv", "env"}:
                        record("env_var", arg, node, index)
                    elif callee == "config":
                        record("config_key", arg, node, index)
                elif (
                    kind == "subscript_expression"
                    and len(node.named_children) > 1
                    and text(data, node.named_children[0]) == "$_ENV"
                ):
                    record("env_var", _literal(data, node.named_children[1]), node, index)
            elif language == "swift":
                if (
                    kind == "call_expression"
                    and node.named_children
                    and text(data, node.named_children[0]) == "ProcessInfo.processInfo.environment"
                ):
                    suffix = next((c for c in node.named_children if c.type == "call_suffix"), None)
                    args = (
                        next(
                            (c for c in suffix.named_children if c.type == "value_arguments"), None
                        )
                        if suffix
                        else None
                    )
                    record("env_var", _literal(data, _first_arg(args)), node, index)
            elif (
                language in {"c", "cpp"}
                and kind == "call_expression"
                and text(data, node.child_by_field_name("function")) in C_ENV_CALLS
            ):
                record(
                    "env_var",
                    _literal(data, _first_arg(node.child_by_field_name("arguments"))),
                    node,
                    index,
                )

    if not found:
        return facts
    ordered = sorted(found.values(), key=lambda c: (c.range.start_byte, c.kind, c.name))
    return facts.model_copy(update={"config": tuple(ordered[:MAX_CONFIG])})


def _first_arg_after_object(node: Node) -> Node | None:
    """Ruby `ENV['X']`: the element after the receiver."""
    return node.named_children[1] if len(node.named_children) > 1 else None
