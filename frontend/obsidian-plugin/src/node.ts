// Electron renderer 里的 Node 能力收口：只有这个文件碰 require，其余模块走它注入的接口。
// isDesktopOnly 插件保证 process/require 存在（移动端没有 Node fs）。
/* eslint-disable @typescript-eslint/no-var-requires */
import type { Stats } from "node:fs";

const req = ((globalThis as Record<string, unknown>).require
  ?? (typeof window !== "undefined" ? (window as unknown as { require?: NodeRequire }).require : undefined)) as NodeRequire;

export const nodeFs = req("fs") as typeof import("node:fs");
export const nodePath = req("path") as typeof import("node:path");

export interface FsLike {
  readFileBuffer(path: string): Promise<Uint8Array>;
  writeFile(path: string, data: Uint8Array | string): Promise<void>;
  mkdirp(path: string): Promise<void>;
  exists(path: string): Promise<boolean>;
  stat(path: string): Promise<Stats | null>;
  readdir(dir: string): Promise<string[]>;
}

export const fs: FsLike = {
  readFileBuffer: (p) => nodeFs.promises.readFile(p).then((b) => new Uint8Array(b)),
  writeFile: (p, d) => nodeFs.promises.writeFile(p, d),
  mkdirp: (p) => nodeFs.promises.mkdir(p, { recursive: true }).then(() => undefined),
  exists: (p) => nodeFs.promises.access(p).then(() => true, () => false),
  stat: (p) => nodeFs.promises.stat(p).then(
    (s) => s,
    () => null,
  ),
  readdir: (d) => nodeFs.promises.readdir(d),
};

export const joinPath: (...segs: string[]) => string = (...segs) => nodePath.join(...segs);
