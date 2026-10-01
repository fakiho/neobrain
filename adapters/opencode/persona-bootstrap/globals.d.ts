// Ambient shims for the standalone strict typecheck — this directory has no
// @types/node and no plugin API types; OpenCode injects both at load time
// (same approach as neoBrain-memory's inline shims). Lives in a .d.ts so
// `declare module` is a declaration, not an augmentation.
declare const process: { env: Record<string, string | undefined> }

declare module "node:fs/promises" {
  export function readFile(path: string, encoding: "utf8"): Promise<string>
}
