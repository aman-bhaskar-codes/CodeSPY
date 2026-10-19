// Trusted compiler fixtures only. This is not the production virtual host.
const assert = require("node:assert/strict");
const test = require("node:test");
const ts = require("typescript");

test("compiler version is pinned independently of the frontend", () => {
  assert.equal(ts.version, "5.9.3");
  assert.match(process.versions.node, /^22\./);
});

test("TSX parse retains UTF16 offsets over original CRLF and astral text", () => {
  const text = "// 😀\r\nconst view = <div/>;\r\n";
  const source = ts.createSourceFile(
    "/view.tsx",
    text,
    ts.ScriptTarget.ES2022,
    true,
    ts.ScriptKind.TSX,
  );
  assert.equal(source.parseDiagnostics.length, 0);
  const element =
    source.statements[0].declarationList.declarations[0].initializer;
  assert.ok(ts.isJsxSelfClosingElement(element));
  assert.equal(text.slice(element.getStart(source), element.end), "<div/>");
  assert.equal(
    Buffer.byteLength(text.slice(0, element.getStart(source))),
    element.getStart(source) + 2,
  );
});

test("checker resolves a local alias using only an explicit virtual host", () => {
  const files = new Map([
    ["/main.ts", "function target() {}\nconst alias = target;\nalias();\n"],
  ]);
  const reads = [];
  const host = {
    getSourceFile(name, target) {
      reads.push(name);
      return files.has(name)
        ? ts.createSourceFile(name, files.get(name), target, true)
        : undefined;
    },
    getDefaultLibFileName: () => "/not-provided.d.ts",
    writeFile: () => assert.fail("emit is forbidden"),
    getCurrentDirectory: () => "/",
    getDirectories: () => [],
    fileExists: (name) => files.has(name),
    readFile: (name) => files.get(name),
    useCaseSensitiveFileNames: () => true,
    getCanonicalFileName: (name) => name,
    getNewLine: () => "\n",
  };
  const program = ts.createProgram(
    ["/main.ts"],
    { noEmit: true, noLib: true, target: ts.ScriptTarget.ES2022 },
    host,
  );
  const source = program.getSourceFile("/main.ts");
  const call = source.statements[2].expression;
  const symbol = program.getTypeChecker().getSymbolAtLocation(call.expression);
  assert.equal(symbol.name, "alias");
  assert.equal(symbol.declarations[0].name.text, "alias");
  assert.ok(reads.every((name) => files.has(name)));
  assert.equal(program.getSyntacticDiagnostics().length, 0);
});
