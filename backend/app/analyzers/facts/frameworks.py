"""Framework facts (HTTP routes, ORM models) layered over per-language facts.

Decorator and annotation based frameworks (Flask, FastAPI, Spring, JPA) read the facts that
were already extracted. Django URLconfs, SQLAlchemy and Django models, Express, Go routers and
gorm need call arguments or struct tags, so they walk the syntax tree again. Every pass is gated
by the file's own imports so unrelated files cost nothing, and every fact cites original bytes.
"""

import re
from collections.abc import Callable

from app.analyzers.facts.common import clip, text
from app.analyzers.positions import SourceIndex
from app.analyzers.registry import parse_bytes
from app.contracts.ast_facts import (
    ColumnFactV1,
    FileFactsV1,
    ModelFactV1,
    RouteFactV1,
    SymbolFactV1,
)
from app.contracts.structure import AnalysisBudget, SourceUnitV1
from app.domain.source_ranges import SourceRangeV1
from tree_sitter import Node

MAX_ROUTES = 256
MAX_MODELS = 256
MAX_COLUMNS = 256
VERBS = ("get", "post", "put", "delete", "patch", "options", "head")
LITERAL = re.compile(r"\"((?:[^\"\\]|\\.)*)\"|'((?:[^'\\]|\\.)*)'")
DECORATOR = re.compile(
    r"^(?P<recv>[A-Za-z_][\w.]*)\.(?P<verb>route|api_route|get|post|put|delete|patch|options|head)"
    r"\((?P<args>.*)\)$",
    re.S,
)
SPRING_METHODS = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
}
GO_ROUTERS = (
    ("gin", "gin-gonic/gin"),
    ("echo", "labstack/echo"),
    ("chi", "go-chi/chi"),
    ("fiber", "gofiber/fiber"),
    ("mux", "gorilla/mux"),
)


def _literals(value: str) -> list[str]:
    return [a or b for a, b in LITERAL.findall(value)]


def _join(base: str, path: str) -> str:
    joined = "/" + "/".join(p.strip("/") for p in (base, path) if p.strip("/"))
    return joined if joined != "/" or (base or path) else "/"


def _modules(facts: FileFactsV1) -> list[str]:
    return [i.module for i in facts.imports]


# ----------------------------------------------------------------------------- Python (symbols)


def _python_decorator_routes(facts: FileFactsV1) -> list[RouteFactV1]:
    heads = {m.split(".")[0] for m in _modules(facts)}
    framework = "fastapi" if "fastapi" in heads else "flask" if "flask" in heads else None
    if framework is None:
        return []
    routes: list[RouteFactV1] = []
    for symbol in facts.symbols:
        if symbol.kind not in {"function", "method"}:
            continue
        for decorator in symbol.decorators:
            match = DECORATOR.match(decorator.strip())
            if match is None:
                continue
            verb, args = match.group("verb"), match.group("args")
            head, _, tail = args.partition("methods")
            literals = _literals(head)
            path = literals[0] if literals else ""
            if verb in {"route", "api_route"}:
                methods = [m.upper() for m in _literals(tail)] or ["GET"]
            else:
                methods = [verb.upper()]
            routes += [
                RouteFactV1(
                    framework=framework,
                    method=method[:16],
                    path=clip(path, 2048),
                    handler=clip(symbol.qualified_name, 2048),
                    range=symbol.range,
                )
                for method in methods
            ]
    return routes


# ------------------------------------------------------------------------------ Java (symbols)


def _java_routes(facts: FileFactsV1) -> list[RouteFactV1]:
    bases: dict[str, str] = {}
    for symbol in facts.symbols:
        if symbol.kind == "class":
            for decorator in symbol.decorators:
                if decorator.startswith("RequestMapping"):
                    found = _literals(decorator)
                    bases[symbol.qualified_name] = found[0] if found else ""
    routes: list[RouteFactV1] = []
    for symbol in facts.symbols:
        if symbol.kind != "method" or "constructor" in symbol.modifiers:
            continue
        for decorator in symbol.decorators:
            name = decorator.split("(", 1)[0].strip()
            if name in SPRING_METHODS:
                methods = [SPRING_METHODS[name]]
            elif name == "RequestMapping":
                methods = re.findall(r"RequestMethod\.(\w+)", decorator) or ["ANY"]
            else:
                continue
            head = decorator.split("method", 1)[0]
            paths = _literals(head) or [""]
            base = bases.get(symbol.container or "", "")
            routes += [
                RouteFactV1(
                    framework="spring",
                    method=method[:16],
                    path=clip(_join(base, path), 2048),
                    handler=clip(symbol.qualified_name, 2048),
                    range=symbol.range,
                )
                for path in paths
                for method in methods
            ]
    return routes


def _java_models(facts: FileFactsV1) -> list[ModelFactV1]:
    fields: dict[str, list[SymbolFactV1]] = {}
    for symbol in facts.symbols:
        if symbol.kind in {"field", "constant"} and symbol.container:
            fields.setdefault(symbol.container, []).append(symbol)
    models: list[ModelFactV1] = []
    for symbol in facts.symbols:
        if symbol.kind != "class" or not any(
            d.split("(", 1)[0].strip() in {"Entity", "MappedSuperclass"} for d in symbol.decorators
        ):
            continue
        table = None
        for decorator in symbol.decorators:
            if decorator.startswith("Table"):
                named = re.search(r"name\s*=\s*\"([^\"]*)\"", decorator)
                table = named.group(1) if named else None
        columns: list[ColumnFactV1] = []
        for field in fields.get(symbol.qualified_name, []):
            if "static" in field.modifiers or any(
                d.startswith("Transient") for d in field.decorators
            ):
                continue
            names = {d.split("(", 1)[0].strip(): d for d in field.decorators}
            column = names.get("Column", "")
            join = names.get("JoinColumn", "")
            declared = re.search(r"name\s*=\s*\"([^\"]*)\"", column or join)
            nullable = re.search(r"nullable\s*=\s*(true|false)", column or join)
            relation = next((n for n in ("ManyToOne", "OneToOne") if n in names), None)
            foreign = None
            if join or relation:
                foreign = (declared.group(1) if declared and join else None) or (
                    field.signature.return_type if field.signature else None
                )
            columns.append(
                ColumnFactV1(
                    name=clip(declared.group(1) if declared else field.name, 256),
                    type=clip(field.signature.return_type, 256)
                    if field.signature and field.signature.return_type
                    else None,
                    nullable=None if nullable is None else nullable.group(1) == "true",
                    primary_key="Id" in names or "EmbeddedId" in names,
                    foreign_key=clip(foreign, 512) if foreign else None,
                )
            )
        models.append(
            ModelFactV1(
                framework="jpa",
                name=clip(symbol.name, 512),
                table=clip(table, 256) if table else None,
                columns=tuple(columns[:MAX_COLUMNS]),
                range=symbol.range,
            )
        )
    return models


# ----------------------------------------------------------------------------- Python (AST)


def _py_string(data: bytes, node: Node | None) -> str | None:
    if node is None or node.type != "string":
        return None
    return "".join(text(data, c) for c in node.named_children if c.type == "string_content")


def _py_call(data: bytes, node: Node) -> tuple[str, list[Node], dict[str, Node]]:
    function = text(data, node.child_by_field_name("function"))
    arguments = node.child_by_field_name("arguments")
    positional: list[Node] = []
    keywords: dict[str, Node] = {}
    for arg in arguments.named_children if arguments is not None else []:
        if arg.type == "keyword_argument":
            name = text(data, arg.child_by_field_name("name"))
            value = arg.child_by_field_name("value")
            if value is not None:
                keywords[name] = value
        else:
            positional.append(arg)
    return function, positional, keywords


def _is_true(data: bytes, node: Node | None) -> bool | None:
    if node is None:
        return None
    value = text(data, node)
    return True if value == "True" else False if value == "False" else None


def _sqlalchemy_column(data: bytes, name: str, annotation: str, call: Node) -> ColumnFactV1 | None:
    function, positional, keywords = _py_call(data, call)
    leaf = function.rsplit(".", 1)[-1]
    if leaf not in {"Column", "mapped_column"}:
        return None
    column_name, type_text, foreign = name, None, None
    for arg in positional:
        if arg.type == "string":
            column_name = _py_string(data, arg) or column_name
        elif arg.type == "call" and text(data, arg.child_by_field_name("function")).endswith(
            "ForeignKey"
        ):
            inner = arg.child_by_field_name("arguments")
            foreign = (
                _py_string(data, inner.named_children[0])
                if inner and inner.named_children
                else None
            )
        elif type_text is None:
            type_text = clip(text(data, arg), 256)
    mapped = re.search(r"Mapped\[(.*)\]\s*$", annotation)
    if type_text is None and mapped:
        type_text = clip(mapped.group(1), 256)
    nullable = _is_true(data, keywords.get("nullable"))
    if nullable is None and mapped:
        nullable = "None" in mapped.group(1) or "Optional" in mapped.group(1)
    return ColumnFactV1(
        name=clip(column_name, 256),
        type=type_text,
        nullable=nullable,
        primary_key=bool(_is_true(data, keywords.get("primary_key"))),
        foreign_key=clip(foreign, 512) if foreign else None,
    )


def _django_column(data: bytes, name: str, call: Node) -> ColumnFactV1 | None:
    function, positional, keywords = _py_call(data, call)
    leaf = function.rsplit(".", 1)[-1]
    if not (function.startswith("models.") or leaf.endswith("Field") or leaf == "ForeignKey"):
        return None
    foreign = None
    if leaf in {"ForeignKey", "OneToOneField"} and positional:
        foreign = _py_string(data, positional[0]) or text(data, positional[0])
    return ColumnFactV1(
        name=clip(name, 256),
        type=clip(leaf, 256),
        nullable=bool(_is_true(data, keywords.get("null"))),
        primary_key=bool(_is_true(data, keywords.get("primary_key"))),
        foreign_key=clip(foreign, 512) if foreign else None,
    )


def _python_ast_facts(
    unit: SourceUnitV1, facts: FileFactsV1, budget: AnalysisBudget
) -> tuple[list[RouteFactV1], list[ModelFactV1]]:
    heads = {m.split(".")[0] for m in _modules(facts)}
    django, sqlalchemy = "django" in heads, bool(heads & {"sqlalchemy", "flask_sqlalchemy"})
    if not (django or sqlalchemy):
        return [], []
    data = unit.data
    index = SourceIndex(data)
    root = parse_bytes("python", data).root_node
    routes: list[RouteFactV1] = []
    models: list[ModelFactV1] = []

    def span(node: Node) -> SourceRangeV1:
        return index.range_from_bytes(node.start_byte, node.end_byte)

    for stmt in root.named_children:
        budget.tick_nodes(1)
        if stmt.type == "expression_statement" and stmt.named_children:
            assignment = stmt.named_children[0]
            if (
                django
                and assignment.type in {"assignment", "augmented_assignment"}
                and text(data, assignment.child_by_field_name("left")) == "urlpatterns"
            ):
                value = assignment.child_by_field_name("right")
                for entry in (
                    value.named_children if value is not None and value.type == "list" else []
                ):
                    budget.tick_nodes(1)
                    if entry.type != "call":
                        continue
                    function, positional, _ = _py_call(data, entry)
                    if (
                        function.rsplit(".", 1)[-1] in {"path", "re_path", "url"}
                        and len(positional) >= 2
                    ):
                        routes.append(
                            RouteFactV1(
                                framework="django",
                                method="ANY",
                                path=clip(_py_string(data, positional[0]) or "", 2048),
                                handler=clip(text(data, positional[1]), 2048),
                                range=span(entry),
                            )
                        )
        elif stmt.type == "class_definition":
            bases = (
                [
                    text(data, b)
                    for b in (stmt.child_by_field_name("superclasses") or stmt).named_children
                ]
                if stmt.child_by_field_name("superclasses")
                else []
            )
            body = stmt.child_by_field_name("body")
            if body is None:
                continue
            is_django = django and any(b == "Model" or b.endswith(".Model") for b in bases)
            columns: list[ColumnFactV1] = []
            table: str | None = None
            has_table = False
            for member in body.named_children:
                budget.tick_nodes(1)
                if (
                    member.type == "class_definition"
                    and text(data, member.child_by_field_name("name")) == "Meta"
                ):
                    for meta in (member.child_by_field_name("body") or member).named_children:
                        inner = meta.named_children[0] if meta.named_children else None
                        if (
                            inner is not None
                            and inner.type == "assignment"
                            and text(data, inner.child_by_field_name("left")) == "db_table"
                        ):
                            table = _py_string(data, inner.child_by_field_name("right")) or table
                    continue
                if member.type != "expression_statement" or not member.named_children:
                    continue
                assignment = member.named_children[0]
                if assignment.type != "assignment":
                    continue
                left = text(data, assignment.child_by_field_name("left"))
                right = assignment.child_by_field_name("right")
                if left == "__tablename__":
                    table = _py_string(data, right) or table
                    has_table = True
                    continue
                if right is None or right.type != "call":
                    continue
                annotation = text(data, assignment.child_by_field_name("type"))
                column = (
                    _django_column(data, left, right)
                    if is_django
                    else _sqlalchemy_column(data, left, annotation, right)
                    if sqlalchemy
                    else None
                )
                if column is not None:
                    columns.append(column)
            if is_django or (sqlalchemy and (has_table or columns)):
                models.append(
                    ModelFactV1(
                        framework="django" if is_django else "sqlalchemy",
                        name=clip(text(data, stmt.child_by_field_name("name")), 512),
                        table=clip(table, 256) if table else None,
                        columns=tuple(columns[:MAX_COLUMNS]),
                        range=span(stmt),
                    )
                )
    return routes, models


# -------------------------------------------------------------------------- JavaScript (AST)


def _express_routes(
    unit: SourceUnitV1, facts: FileFactsV1, budget: AnalysisBudget
) -> list[RouteFactV1]:
    if not any(m == "express" or m.startswith("express/") for m in _modules(facts)):
        return []
    data = unit.data
    index = SourceIndex(data)
    root = parse_bytes(unit.language, data).root_node
    routes: list[RouteFactV1] = []
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        budget.tick_nodes(1)
        stack.extend(reversed(node.children))
        if node.type != "call_expression" or len(routes) >= MAX_ROUTES:
            continue
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if function is None or function.type != "member_expression" or arguments is None:
            continue
        verb = text(data, function.child_by_field_name("property"))
        if verb not in {*VERBS, "all", "use"} or not arguments.named_children:
            continue
        first = arguments.named_children[0]
        if first.type != "string":
            continue
        path = "".join(text(data, c) for c in first.named_children if c.type == "string_fragment")
        last = arguments.named_children[-1]
        if last is first:
            continue
        handler = (
            "<anonymous>"
            if last.type in {"arrow_function", "function_expression", "function"}
            else text(data, last)
        )
        routes.append(
            RouteFactV1(
                framework="express",
                method=verb.upper()[:16],
                path=clip(path, 2048),
                handler=clip(handler, 2048),
                range=index.range_from_bytes(node.start_byte, node.end_byte),
            )
        )
    return routes


# ------------------------------------------------------------------------------- Go (AST)


def _snake(name: str) -> str:
    step = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", step).lower()


def _go_string(data: bytes, node: Node | None) -> str | None:
    if node is None:
        return None
    if node.type == "interpreted_string_literal":
        return "".join(
            text(data, c)
            for c in node.named_children
            if c.type == "interpreted_string_literal_content"
        )
    if node.type == "raw_string_literal":
        return "".join(
            text(data, c) for c in node.named_children if c.type == "raw_string_literal_content"
        )
    return None


def _go_facts(
    unit: SourceUnitV1, facts: FileFactsV1, budget: AnalysisBudget
) -> tuple[list[RouteFactV1], list[ModelFactV1]]:
    imports = _modules(facts)
    router = next((name for name, marker in GO_ROUTERS if any(marker in m for m in imports)), None)
    http_mux = any(m == "net/http" for m in imports)
    gorm = any("gorm.io" in m for m in imports)
    if not (router or http_mux or gorm):
        return [], []
    data = unit.data
    index = SourceIndex(data)
    root = parse_bytes("go", data).root_node
    routes: list[RouteFactV1] = []
    models: list[ModelFactV1] = []
    table_names: dict[str, str] = {}
    structs: list[Node] = []

    def span(node: Node) -> SourceRangeV1:
        return index.range_from_bytes(node.start_byte, node.end_byte)

    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        budget.tick_nodes(1)
        stack.extend(reversed(node.children))
        if node.type == "call_expression" and len(routes) < MAX_ROUTES:
            function = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            if function is None or function.type != "selector_expression" or arguments is None:
                continue
            name = text(data, function.child_by_field_name("field"))
            operand = text(data, function.child_by_field_name("operand"))
            args = arguments.named_children
            path = _go_string(data, args[0]) if args else None
            if path is None or len(args) < 2:
                continue
            handler = text(data, args[-1])
            if args[-1].type == "func_literal":
                handler = "<anonymous>"
            if name in {"HandleFunc", "Handle"} and (http_mux or router):
                method, _, rest = path.partition(" ")
                if rest and method.isupper():
                    method, path = method, rest.strip()
                else:
                    method = "ANY"
                label = "net/http" if operand == "http" or not router else router
            elif router and name.lower() in VERBS and operand != "http":
                method, label = name.upper(), router
            else:
                continue
            routes.append(
                RouteFactV1(
                    framework=label,
                    method=method[:16],
                    path=clip(path, 2048),
                    handler=clip(handler, 2048),
                    range=span(node),
                )
            )
        elif gorm and node.type == "method_declaration":
            if text(data, node.child_by_field_name("name")) == "TableName":
                receiver = node.child_by_field_name("receiver")
                decl = next((c for c in receiver.named_children), None) if receiver else None
                owner = text(data, decl.child_by_field_name("type")).lstrip("*") if decl else ""
                literal = next((n for n in _descendants(node, "interpreted_string_literal")), None)
                value = _go_string(data, literal)
                if owner and value:
                    table_names[owner] = value
        elif gorm and node.type == "type_spec":
            body = node.child_by_field_name("type")
            if body is not None and body.type == "struct_type":
                structs.append(node)
    for spec in structs:
        model = _gorm_model(data, spec, table_names, span)
        if model is not None and len(models) < MAX_MODELS:
            models.append(model)
    return routes, models


def _descendants(node: Node, kind: str) -> list[Node]:
    found: list[Node] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == kind:
            found.append(current)
        stack.extend(reversed(current.children))
    return found


def _gorm_model(
    data: bytes,
    spec: Node,
    table_names: dict[str, str],
    span: Callable[[Node], SourceRangeV1],
) -> ModelFactV1 | None:
    name = text(data, spec.child_by_field_name("name"))
    struct = spec.child_by_field_name("type")
    members = (
        next((c for c in struct.named_children if c.type == "field_declaration_list"), None)
        if struct
        else None
    )
    if members is None:
        return None
    columns: list[ColumnFactV1] = []
    tagged = embedded = False
    for field in members.named_children:
        if field.type != "field_declaration":
            continue
        type_node = field.child_by_field_name("type")
        type_text = clip(text(data, type_node), 256)
        names = [n for n in field.children_by_field_name("name") if n.is_named]
        tag = _go_string(data, field.child_by_field_name("tag")) or ""
        gorm_tag = re.search(r'gorm:"([^"]*)"', tag)
        if not names:
            if type_text == "gorm.Model":
                embedded = True
                columns += [
                    ColumnFactV1(name="id", type="uint", primary_key=True),
                    ColumnFactV1(name="created_at", type="time.Time"),
                    ColumnFactV1(name="updated_at", type="time.Time"),
                    ColumnFactV1(name="deleted_at", type="gorm.DeletedAt", nullable=True),
                ]
            continue
        spec_text = gorm_tag.group(1) if gorm_tag else ""
        if gorm_tag:
            tagged = True
        if spec_text.strip() == "-":
            continue
        for field_name in names:
            ident = text(data, field_name)
            if not ident[:1].isupper():
                continue
            column = re.search(r"column:([\w]+)", spec_text)
            foreign = re.search(r"foreignKey:([\w]+)", spec_text)
            columns.append(
                ColumnFactV1(
                    name=clip(column.group(1) if column else _snake(ident), 256),
                    type=type_text or None,
                    nullable=True
                    if type_text.startswith("*")
                    else False
                    if "not null" in spec_text.lower()
                    else None,
                    primary_key=bool(re.search(r"primary_?key", spec_text, re.I)),
                    foreign_key=clip(foreign.group(1), 512) if foreign else None,
                )
            )
    if not (tagged or embedded):
        return None
    return ModelFactV1(
        framework="gorm",
        name=clip(name, 512),
        table=clip(table_names[name], 256) if name in table_names else None,
        columns=tuple(columns[:MAX_COLUMNS]),
        range=span(spec),
    )


# ----------------------------------------------------------------------------------- entry point


def enrich(unit: SourceUnitV1, facts: FileFactsV1, budget: AnalysisBudget) -> FileFactsV1:
    routes: list[RouteFactV1] = []
    models: list[ModelFactV1] = []
    language = unit.language
    if language == "python":
        routes += _python_decorator_routes(facts)
        ast_routes, ast_models = _python_ast_facts(unit, facts, budget)
        routes += ast_routes
        models += ast_models
    elif language in {"javascript", "jsx", "typescript", "tsx"}:
        routes += _express_routes(unit, facts, budget)
    elif language == "java":
        routes += _java_routes(facts)
        models += _java_models(facts)
    elif language == "go":
        go_routes, go_models = _go_facts(unit, facts, budget)
        routes += go_routes
        models += go_models
    if not routes and not models:
        return facts
    routes.sort(key=lambda r: (r.range.start_byte, r.method, r.path))
    models.sort(key=lambda m: m.range.start_byte)
    return facts.model_copy(
        update={"routes": tuple(routes[:MAX_ROUTES]), "models": tuple(models[:MAX_MODELS])}
    )
