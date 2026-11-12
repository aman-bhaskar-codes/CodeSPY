import hashlib
from uuid import uuid4

import pytest
from app.analyzers.facts import extract_facts
from app.analyzers.positions import SourceIndex
from app.contracts.structure import AnalysisBudget, SourceUnitV1

FLASK = """from flask import Flask, Blueprint
app = Flask(__name__)
bp = Blueprint("x", __name__)

@app.route("/users/<int:id>", methods=["GET", "POST"])
def user(id): pass

@bp.get("/items")
def items(): pass

@app.route("/home")
def home(): pass
"""

FASTAPI = """from fastapi import FastAPI, APIRouter
app = FastAPI()
router = APIRouter()

@app.get("/health")
async def health(): pass

@router.post("/orders", status_code=201)
def create_order(): pass
"""

DJANGO_URLS = """from django.urls import path, re_path, include
from . import views
urlpatterns = [
    path("a/", views.a, name="a"),
    re_path(r"^b$", views.B.as_view()),
]
urlpatterns += [path("c/", include("x.urls"))]
"""

DJANGO_MODELS = """from django.db import models
class Product(models.Model):
    name = models.CharField(max_length=50)
    price = models.DecimalField(max_digits=5, decimal_places=2, null=True)
    owner = models.ForeignKey("User", on_delete=models.CASCADE)
    sku = models.CharField(max_length=10, primary_key=True)
    class Meta:
        db_table = "products"
"""

SQLALCHEMY = """from sqlalchemy import Column, Integer, String, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, DeclarativeBase
class Base(DeclarativeBase): pass
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    name = Column(String(50), nullable=False)
    team_id = Column(Integer, ForeignKey("teams.id"))
class Team(Base):
    __tablename__ = "teams"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(30))
    note: Mapped[str | None] = mapped_column(String)
"""

EXPRESS = """const express = require("express");
const app = express();
app.get("/users/:id", auth, (req, res) => {});
router.post("/x", handler);
app.use("/api", router);
app.use(logger);
"""

SPRING = """package com.ex;
@RestController
@RequestMapping("/api/users")
public class UserController {
  @GetMapping("/{id}") public User get(@PathVariable int id) { return null; }
  @PostMapping public User create() { return null; }
  @RequestMapping(value = "/x", method = RequestMethod.PUT) public void put() {}
  public void helper() {}
}
@Entity
@Table(name = "users")
public class User {
  @Id private Long id;
  @Column(name = "full_name", nullable = false) private String name;
  @ManyToOne @JoinColumn(name = "team_id") private Team team;
  private static int counter;
  @Transient private String temp;
}
"""

GO = """package main
import (
  "net/http"
  "github.com/gin-gonic/gin"
  "gorm.io/gorm"
)
func main() {
  r := gin.Default()
  r.GET("/ping", ping)
  api := r.Group("/api")
  api.POST("/users", func(c *gin.Context) {})
  http.HandleFunc("GET /y", h2)
  http.Get("https://example.com/x")
}
type User struct {
  gorm.Model
  Name string `gorm:"column:full_name;not null"`
  Age  *int
  Email string `gorm:"uniqueIndex"`
  TeamID uint `gorm:"foreignKey:ID"`
  Skip string `gorm:"-"`
}
func (User) TableName() string { return "people" }
type Plain struct { Name string }
"""


def facts(source, language, path):
    data = source.encode()
    unit = SourceUnitV1(
        file_id=uuid4(),
        path=path,
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
        language=language,
    )
    return unit, extract_facts(unit, AnalysisBudget(max_syntax_nodes=200000))


def routes(result):
    return [(r.framework, r.method, r.path, r.handler) for r in result.routes]


def columns(model):
    return [(c.name, c.type, c.nullable, c.primary_key, c.foreign_key) for c in model.columns]


def test_flask_decorators_expand_methods_and_default_to_get():
    _, result = facts(FLASK, "python", "app/web.py")
    assert routes(result) == [
        ("flask", "GET", "/users/<int:id>", "app.web.user"),
        ("flask", "POST", "/users/<int:id>", "app.web.user"),
        ("flask", "GET", "/items", "app.web.items"),
        ("flask", "GET", "/home", "app.web.home"),
    ]


def test_fastapi_decorators():
    _, result = facts(FASTAPI, "python", "app/api.py")
    assert routes(result) == [
        ("fastapi", "GET", "/health", "app.api.health"),
        ("fastapi", "POST", "/orders", "app.api.create_order"),
    ]


def test_django_urlconf_routes_include_regex_views_and_include():
    _, result = facts(DJANGO_URLS, "python", "shop/urls.py")
    assert routes(result) == [
        ("django", "ANY", "a/", "views.a"),
        ("django", "ANY", "^b$", "views.B.as_view()"),
        ("django", "ANY", "c/", 'include("x.urls")'),
    ]


def test_django_models_columns_and_meta_table():
    _, result = facts(DJANGO_MODELS, "python", "shop/models.py")
    (model,) = result.models
    assert (model.framework, model.name, model.table) == ("django", "Product", "products")
    assert columns(model) == [
        ("name", "CharField", False, False, None),
        ("price", "DecimalField", True, False, None),
        ("owner", "ForeignKey", False, False, "User"),
        ("sku", "CharField", False, True, None),
    ]


def test_sqlalchemy_classic_and_mapped_columns():
    _, result = facts(SQLALCHEMY, "python", "db/models.py")
    users, teams = result.models
    assert (users.framework, users.name, users.table) == ("sqlalchemy", "User", "users")
    assert columns(users) == [
        ("id", "Integer", None, True, None),
        ("name", "String(50)", False, False, None),
        ("team_id", "Integer", None, False, "teams.id"),
    ]
    assert (teams.name, teams.table) == ("Team", "teams")
    assert columns(teams) == [
        ("id", "int", False, True, None),
        ("title", "String(30)", False, False, None),
        ("note", "String", True, False, None),
    ]
    assert [m.name for m in result.models] == ["User", "Team"]  # the bare Base class is not a model


def test_express_routes_including_use_and_anonymous_handlers():
    _, result = facts(EXPRESS, "javascript", "srv/app.js")
    assert routes(result) == [
        ("express", "GET", "/users/:id", "<anonymous>"),
        ("express", "POST", "/x", "handler"),
        ("express", "USE", "/api", "router"),
    ]


def test_spring_routes_join_class_and_method_paths():
    _, result = facts(SPRING, "java", "src/main/java/com/ex/UserController.java")
    assert routes(result) == [
        ("spring", "GET", "/api/users/{id}", "com.ex.UserController.get"),
        ("spring", "POST", "/api/users", "com.ex.UserController.create"),
        ("spring", "PUT", "/api/users/x", "com.ex.UserController.put"),
    ]


def test_jpa_entity_columns_keys_and_exclusions():
    _, result = facts(SPRING, "java", "src/main/java/com/ex/UserController.java")
    (model,) = result.models
    assert (model.framework, model.name, model.table) == ("jpa", "User", "users")
    assert columns(model) == [
        ("id", "Long", None, True, None),
        ("full_name", "String", False, False, None),
        ("team_id", "Team", None, False, "team_id"),
    ]  # static and @Transient fields are not columns


def test_go_routes_distinguish_router_net_http_and_client_calls():
    _, result = facts(GO, "go", "srv/main.go")
    assert routes(result) == [
        ("gin", "GET", "/ping", "ping"),
        ("gin", "POST", "/users", "<anonymous>"),
        ("net/http", "GET", "/y", "h2"),
    ]  # http.Get(url) is a client call; Group prefixes are not resolved


def test_gorm_model_embedded_tags_pointers_and_table_name():
    _, result = facts(GO, "go", "srv/main.go")
    (model,) = result.models
    assert (model.framework, model.name, model.table) == ("gorm", "User", "people")
    assert columns(model) == [
        ("id", "uint", None, True, None),
        ("created_at", "time.Time", None, False, None),
        ("updated_at", "time.Time", None, False, None),
        ("deleted_at", "gorm.DeletedAt", True, False, None),
        ("full_name", "string", False, False, None),
        ("age", "*int", True, False, None),
        ("email", "string", None, False, None),
        ("team_id", "uint", None, False, "ID"),
    ]  # gorm:"-" is skipped; Plain has no gorm markers and is not a model


@pytest.mark.parametrize(
    ("language", "path", "source"),
    [
        ("python", "a.py", '@app.route("/x")\ndef f(): pass\n'),  # no flask/fastapi import
        ("python", "a.py", 'urlpatterns = [path("a/", v)]\n'),  # no django import
        ("python", "a.py", "class A:\n    x = Column(Integer)\n"),  # no sqlalchemy import
        ("javascript", "a.js", 'app.get("/x", h);\n'),  # no express import
        ("go", "a.go", 'package a\nfunc f() { r.GET("/x", h) }\n'),  # no router import
        (
            "java",
            "A.java",
            'class A { @GetMapping("/x") void f() {} }\n',
        ),  # no @Entity: models only
    ],
)
def test_framework_facts_require_the_framework_to_be_imported(language, path, source):
    _, result = facts(source, language, path)
    assert result.models == ()
    if language != "java":
        assert result.routes == ()


def test_plain_files_carry_no_framework_facts():
    for language, path, source in [
        ("python", "a.py", "def f(): pass\n"),
        ("rust", "a.rs", "fn f() {}\n"),
        ("typescript", "a.ts", "export const x = 1;\n"),
    ]:
        _, result = facts(source, language, path)
        assert result.routes == () and result.models == ()


SAMPLES = [
    ("python", "app/web.py", FLASK),
    ("python", "shop/urls.py", DJANGO_URLS),
    ("python", "db/models.py", SQLALCHEMY),
    ("javascript", "srv/app.js", EXPRESS),
    ("java", "src/main/java/com/ex/UserController.java", SPRING),
    ("go", "srv/main.go", GO),
]


@pytest.mark.parametrize(("language", "path", "source"), SAMPLES)
def test_every_framework_fact_cites_original_bytes_and_is_deterministic(language, path, source):
    unit, result = facts(source, language, path)
    index = SourceIndex(unit.data)
    cited = [*result.routes, *result.models]
    assert cited
    for item in cited:
        index.validate_range(item.range)
    again = extract_facts(unit, AnalysisBudget(max_syntax_nodes=200000))
    assert result.facts_sha256() == again.facts_sha256()


def test_route_count_is_capped_per_file():
    body = "".join(f'app.get("/r{i}", h{i});\n' for i in range(400))
    _, result = facts('const express = require("express");\n' + body, "javascript", "big.js")
    assert len(result.routes) == 256
