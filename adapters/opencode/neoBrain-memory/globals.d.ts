// Ambient shim for the standalone strict typecheck — no @types/node here;
// OpenCode injects the real module at load time. (Must live in a .d.ts:
// `declare module` inside a module file is an augmentation and errors.)
declare module "node:fs/promises" {
  export function readFile(path: string, encoding: "utf8"): Promise<string>
}
