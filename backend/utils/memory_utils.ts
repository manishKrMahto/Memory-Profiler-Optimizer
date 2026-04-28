export function formatMemory(bytes: number): string {
  return (bytes / 1024 / 1024).toFixed(2) + " MB";
}

export function safeGc(): void {
  const g = global as unknown as { gc?: () => void };
  if (typeof g.gc === "function") g.gc();
}

export function heapUsedBytes(): number {
  return process.memoryUsage().heapUsed;
}

