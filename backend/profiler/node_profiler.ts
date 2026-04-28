import fs from "node:fs";
import path from "node:path";
import { performance } from "node:perf_hooks";
import ts from "typescript";
import { heapUsedBytes, safeGc } from "../utils/memory_utils";

type ProfilePayload = {
  file_path?: string;
  code?: string;
  qualname: string;
  timeout_s?: number;
  trigger_gc?: boolean;
  language?: "js" | "ts";
  sample_interval_ms?: number;
};

type ProfileResult = {
  memory_usage: number[]; // MB samples (best-effort)
  peak_memory: number | null; // MB
  execution_time: number | null; // seconds
  profiler_output: string;
  error: string;
};

function mb(bytes: number): number {
  return bytes / 1024 / 1024;
}

function transpileIfNeeded(code: string, language: "js" | "ts"): string {
  if (language === "js") return code;
  const out = ts.transpileModule(code, {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.CommonJS,
      esModuleInterop: true,
      sourceMap: false,
    },
  });
  return out.outputText;
}

function resolveFromExportsOrGlobal(modExports: any, qualname: string): any {
  const parts = (qualname || "").split(".").filter(Boolean);
  const roots = [modExports, (globalThis as any)];
  for (const root of roots) {
    let cur: any = root;
    let ok = true;
    for (const p of parts) {
      if (cur == null) {
        ok = false;
        break;
      }
      cur = cur[p];
    }
    if (ok && cur != null) return cur;
  }
  return undefined;
}

function buildCallableModuleFromCode(jsCode: string): { exports: any; error?: string } {
  // Minimal CommonJS wrapper to evaluate code and let it attach to module.exports if it wants.
  const module = { exports: {} as any };
  const exports = module.exports;

  try {
    // eslint-disable-next-line no-new-func
    const fn = new Function("exports", "module", "require", "__filename", "__dirname", jsCode);
    fn(exports, module, require, "<mpo-snippet>", process.cwd());
    return { exports: module.exports };
  } catch (e: any) {
    return { exports: {}, error: String(e?.stack || e?.message || e) };
  }
}

async function runProfile(payload: ProfilePayload): Promise<ProfileResult> {
  const triggerGc = payload.trigger_gc !== false; // default true
  const intervalMs = Math.max(5, Number(payload.sample_interval_ms ?? 25));

  // Be defensive: callers may send non-string JSON values.
  let code = typeof payload.code === "string" ? payload.code : payload.code == null ? "" : String(payload.code);
  let language: "js" | "ts" = payload.language ?? "js";
  if (!code && payload.file_path) {
    const abs = path.resolve(payload.file_path);
    code = fs.readFileSync(abs, "utf-8");
    const ext = path.extname(abs).toLowerCase();
    language = ext === ".ts" || ext === ".tsx" ? "ts" : "js";
  }

  if (!code) {
    return { memory_usage: [], peak_memory: null, execution_time: null, profiler_output: "", error: "missing_code" };
  }

  const jsCode = transpileIfNeeded(code, language);
  const built = buildCallableModuleFromCode(jsCode);
  if (built.error) {
    return { memory_usage: [], peak_memory: null, execution_time: null, profiler_output: "", error: built.error };
  }

  const fn = resolveFromExportsOrGlobal(built.exports, payload.qualname);
  if (typeof fn !== "function") {
    // Best-effort: if function isn't exported, try to eval it from the same code by re-wrapping.
    // This supports top-level function declarations in a controlled wrapper scope.
    try {
      // eslint-disable-next-line no-new-func
      const getter = new Function(
        `"use strict";\n${jsCode}\nreturn (function(){ try { return (${payload.qualname}); } catch { return undefined; } })();`,
      );
      const maybe = getter();
      if (typeof maybe === "function") {
        return await profileCallable(maybe, payload.qualname, triggerGc, intervalMs);
      }
    } catch {
      // ignore
    }
    return {
      memory_usage: [],
      peak_memory: null,
      execution_time: null,
      profiler_output: "",
      error: `Function not resolvable: ${payload.qualname}`,
    };
  }

  return await profileCallable(fn, payload.qualname, triggerGc, intervalMs);
}

async function profileCallable(fn: (...args: any[]) => any, name: string, triggerGc: boolean, intervalMs: number): Promise<ProfileResult> {
  const samples: number[] = [];

  if (triggerGc) safeGc();
  const beforeBytes = heapUsedBytes();
  samples.push(mb(beforeBytes));

  let timer: NodeJS.Timeout | null = null;
  try {
    timer = setInterval(() => {
      samples.push(mb(heapUsedBytes()));
    }, intervalMs);

    const t0 = performance.now();
    const res = fn(); // no-arg by design
    if (res && typeof (res as any).then === "function") {
      await res;
    }
    const t1 = performance.now();

    if (timer) clearInterval(timer);
    if (triggerGc) safeGc();
    const afterBytes = heapUsedBytes();
    samples.push(mb(afterBytes));

    const peak = samples.length ? Math.max(...samples) : mb(afterBytes);
    const diffKb = (afterBytes - beforeBytes) / 1024;
    const spikeDetected = diffKb >= 1024; // default heuristic: >= 1MB diff

    return {
      memory_usage: samples,
      peak_memory: peak,
      execution_time: (t1 - t0) / 1000,
      profiler_output: JSON.stringify(
        {
          function_name: name,
          memory_before: beforeBytes,
          memory_after: afterBytes,
          memory_diff_kb: Math.round(diffKb),
          execution_time_ms: Math.round(t1 - t0),
          spike_detected: spikeDetected,
          note: "V8 GC is non-deterministic; memory may not drop immediately.",
        },
        null,
        2,
      ),
      error: "",
    };
  } catch (e: any) {
    if (timer) clearInterval(timer);
    return {
      memory_usage: samples,
      peak_memory: samples.length ? Math.max(...samples) : null,
      execution_time: null,
      profiler_output: "",
      error: String(e?.stack || e?.message || e),
    };
  }
}

async function main() {
  const payload = JSON.parse(fs.readFileSync(0, "utf-8") || "{}") as ProfilePayload;
  const out = await runProfile(payload);
  process.stdout.write(JSON.stringify(out));
}

main().catch((e) => {
  process.stdout.write(
    JSON.stringify({
      memory_usage: [],
      peak_memory: null,
      execution_time: null,
      profiler_output: "",
      error: String((e as any)?.stack || (e as any)?.message || e),
    }),
  );
});

