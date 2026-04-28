import fs from "node:fs";
import path from "node:path";
import ts from "typescript";

type FunctionMeta = {
  function_name: string;
  code: string;
  start_line: number;
  end_line: number;
  file_path: string; // repo-relative posix
  can_call_without_args: boolean;
};

function toPosix(p: string): string {
  return p.replaceAll("\\", "/");
}

function relPosix(absFile: string, repoRoot?: string): string {
  if (!repoRoot) return toPosix(path.basename(absFile));
  try {
    return toPosix(path.relative(repoRoot, absFile));
  } catch {
    return toPosix(path.basename(absFile));
  }
}

function getLines(src: string): string[] {
  // Keepends is not directly available; we compute slices via positions instead.
  return src.split(/\r?\n/);
}

function lineFromPos(sf: ts.SourceFile, pos: number): number {
  return sf.getLineAndCharacterOfPosition(pos).line + 1;
}

function textForNode(src: string, node: ts.Node): string {
  // Use getFullStart to include leading trivia for function declarations is noisy; use node.getStart for cleaner code blocks.
  const start = node.getStart();
  const end = node.getEnd();
  return src.slice(start, end);
}

function canCallWithoutArgs(node: ts.SignatureDeclarationBase): boolean {
  const params = node.parameters ?? [];
  for (const p of params) {
    // Rest param requires args (or can be called with none); but semantics vary; treat as callable with no args.
    if (p.dotDotDotToken) continue;
    if (p.questionToken) continue;
    if (p.initializer) continue;
    return false;
  }
  return true;
}

function isExported(node: ts.Node): boolean {
  const mods = (node as any).modifiers as ts.NodeArray<ts.Modifier> | undefined;
  return !!mods?.some((m) => m.kind === ts.SyntaxKind.ExportKeyword);
}

function collectFunctionMetas(absFile: string, src: string, repoRoot?: string): FunctionMeta[] {
  const ext = path.extname(absFile).toLowerCase();
  const scriptKind =
    ext === ".ts" || ext === ".tsx"
      ? ts.ScriptKind.TS
      : ext === ".js" || ext === ".jsx"
        ? ts.ScriptKind.JS
        : ts.ScriptKind.Unknown;

  const sf = ts.createSourceFile(absFile, src, ts.ScriptTarget.ES2022, true, scriptKind);
  const out: FunctionMeta[] = [];
  const rel = relPosix(absFile, repoRoot);

  function add(name: string, node: ts.Node, sigNode: ts.SignatureDeclarationBase) {
    const startLine = lineFromPos(sf, node.getStart());
    const endLine = lineFromPos(sf, node.getEnd());
    out.push({
      function_name: name,
      code: textForNode(src, node) + (src.slice(node.getEnd(), node.getEnd() + 1) === "\n" ? "" : "\n"),
      start_line: startLine,
      end_line: endLine,
      file_path: rel,
      can_call_without_args: canCallWithoutArgs(sigNode),
    });
  }

  function visit(node: ts.Node, classStack: string[]) {
    if (ts.isClassDeclaration(node) && node.name) {
      const next = [...classStack, node.name.text];
      node.members.forEach((m) => visit(m, next));
      return;
    }

    // function foo() {}
    if (ts.isFunctionDeclaration(node) && node.name) {
      add(node.name.text, node, node);
      return;
    }

    // class C { method() {} }
    if (ts.isMethodDeclaration(node) && node.name && ts.isIdentifier(node.name) && classStack.length) {
      add(`${classStack.join(".")}.${node.name.text}`, node, node);
      return;
    }

    // const foo = () => {}  / const foo = function() {}
    if (ts.isVariableStatement(node)) {
      for (const decl of node.declarationList.declarations) {
        if (!ts.isIdentifier(decl.name) || !decl.initializer) continue;
        const nm = decl.name.text;
        const init = decl.initializer;
        if (ts.isArrowFunction(init) || ts.isFunctionExpression(init)) {
          // Use the initializer span for code readability; we still qualify by variable name.
          add(nm, init, init);
        }
      }
      return;
    }

    // export default function () {} -> can't name reliably; ignore.
    ts.forEachChild(node, (c) => visit(c, classStack));
  }

  visit(sf, []);
  return out;
}

function main() {
  const payload = JSON.parse(fs.readFileSync(0, "utf-8") || "{}") as {
    file_path: string;
    repo_root?: string;
  };
  const abs = path.resolve(payload.file_path);
  const src = fs.readFileSync(abs, "utf-8");
  const metas = collectFunctionMetas(abs, src, payload.repo_root ? path.resolve(payload.repo_root) : undefined);
  process.stdout.write(JSON.stringify({ functions: metas }));
}

main();

