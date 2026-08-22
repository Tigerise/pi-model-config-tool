// 供 pi 模型配置工具借 Node 引擎发 HTTP 请求（部分中转只放行 Node 客户端）。
// 用法：node nodefetch.mjs，请求内容用 JSON 从标准输入传进来：
//   {"url":"...","headers":{...},"method":"GET","body":null,"timeout":25}
// 密钥走标准输入而不走命令行参数，避免同机其他进程从进程命令行里看到。
// 输出：第一行 @@STATUS@@<状态码>，其余为响应正文；请求失败时状态码为 -1。
let raw = "";
for await (const chunk of process.stdin) raw += chunk;

let req;
try {
  req = JSON.parse(raw || "{}");
} catch (e) {
  console.log("@@STATUS@@-1");
  console.log("输入不是合法 JSON：" + String((e && e.message) || e));
  process.exit(0);
}

const timeoutMs = (parseInt(req.timeout, 10) || 25) * 1000;
const ctrl = new AbortController();
const timer = setTimeout(() => ctrl.abort(), timeoutMs);
try {
  const method = req.method || "GET";
  const opt = {
    method,
    headers: req.headers || {},
    signal: ctrl.signal,
  };
  if (req.body != null && method !== "GET") {
    opt.body = req.body;
  }
  const res = await fetch(req.url, opt);
  const text = await res.text();
  clearTimeout(timer);
  console.log("@@STATUS@@" + res.status);
  console.log(text);
} catch (e) {
  clearTimeout(timer);
  console.log("@@STATUS@@-1");
  console.log(String((e && e.message) || e));
}
