// Obsidian 插件标准打包：cjs、外部化 obsidian/electron/node builtins（宿主提供）。
import esbuild from "esbuild";

const prod = process.argv[2] === "production";

await esbuild.build({
  entryPoints: ["src/main.ts"],
  bundle: true,
  outfile: "main.js",
  format: "cjs",
  target: "es2022",
  platform: "browser", // Electron renderer：fetch/fs 走 require，见 src/node.ts
  sourcemap: prod ? false : "inline",
  minify: prod,
  external: ["obsidian", "electron", "fs", "path", "crypto", "os"],
});
