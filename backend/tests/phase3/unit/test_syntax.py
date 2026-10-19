import hashlib
from uuid import uuid4

import pytest
from app.analyzers.languages import extract_syntax
from app.contracts.structure import AnalysisBudget, SourceUnitV1


def make_unit(source: str, language: str, path: str) -> SourceUnitV1:
    data = source.encode("utf-8")
    return SourceUnitV1(
        file_id=uuid4(),
        path=path,
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
        language=language,  # type: ignore[arg-type]
    )


def test_python_syntax_extraction():
    src = """import os
from sys import argv

class Greeter:
    def __init__(self, name: str):
        self.name = name

    async def greet(self):
        print(self.name)

g = Greeter("world")
g.greet()
"""
    unit = make_unit(src, "python", "src/greeter.py")
    budget = AnalysisBudget(max_syntax_nodes=10000)
    obs = extract_syntax(unit, budget)

    assert obs.language == "python"
    assert not obs.has_syntax_errors
    kinds = [s.kind for s in obs.symbols]
    assert "module" in kinds
    assert "class" in kinds
    assert "method" in kinds
    assert "import" in kinds
    assert "call" in kinds

    method_syms = [s for s in obs.symbols if s.kind == "method"]
    greet_method = next(m for m in method_syms if m.name == "greet")
    assert greet_method.is_async is True


def test_javascript_jsx_syntax_extraction():
    src = """import React from 'react';

class App extends React.Component {
    render() {
        return <div onClick={this.handleClick}>Hello</div>;
    }
}
function handleClick() {
    console.log('clicked');
}
handleClick();
"""
    unit = make_unit(src, "jsx", "src/App.jsx")
    budget = AnalysisBudget(max_syntax_nodes=10000)
    obs = extract_syntax(unit, budget)

    assert obs.language == "jsx"
    assert not obs.has_syntax_errors
    names = [s.name for s in obs.symbols]
    assert "App" in names
    assert "handleClick" in names


def test_typescript_tsx_syntax_extraction():
    src = """import type { FC } from 'react';

interface Props {
    title: string;
}

export const Header: FC<Props> = ({ title }) => {
    return <h1>{title}</h1>;
};
"""
    unit = make_unit(src, "tsx", "src/Header.tsx")
    budget = AnalysisBudget(max_syntax_nodes=10000)
    obs = extract_syntax(unit, budget)

    assert obs.language == "tsx"
    assert not obs.has_syntax_errors
    type_imports = [s for s in obs.symbols if s.kind == "import" and s.type_only]
    assert len(type_imports) >= 1


def test_syntax_error_reported():
    src = "def bad_syntax(:\n    pass\n"
    unit = make_unit(src, "python", "src/broken.py")
    budget = AnalysisBudget(max_syntax_nodes=10000)
    obs = extract_syntax(unit, budget)

    assert obs.has_syntax_errors is True
    assert len(obs.diagnostics) >= 1
    assert obs.diagnostics[0].code == "syntax_error"


def test_budget_exhaustion_raises():
    src = "x = 1\ny = 2\nz = 3\n"
    unit = make_unit(src, "python", "src/budget.py")
    budget = AnalysisBudget(max_syntax_nodes=2)
    with pytest.raises(TimeoutError, match="syntax_node_budget_exceeded"):
        extract_syntax(unit, budget)
