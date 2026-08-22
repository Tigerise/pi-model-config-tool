// 用 pi 自带的校验器检查一份 models.json，避免工具写出 pi 读不了的配置。
// 用法：node pivalidate.mjs <pi的dist目录> <models.json路径>
// 输出：一行 JSON，{"ok":true,"providers":[...]} 或 {"ok":false,"error":"..."}
import { pathToFileURL } from "node:url";
import path from "node:path";

const [distDir, jsonPath] = process.argv.slice(2);
try {
  const mod = await import(
    pathToFileURL(path.join(distDir, "core", "model-config.js")).href
  );
  const cfg = await mod.ModelConfig.load(jsonPath);
  const err = cfg.getError();
  if (err) {
    console.log(JSON.stringify({ ok: false, error: String(err) }));
  } else {
    console.log(JSON.stringify({ ok: true, providers: cfg.getProviderIds() }));
  }
} catch (e) {
  console.log(
    JSON.stringify({ ok: null, error: String((e && e.message) || e) })
  );
}
