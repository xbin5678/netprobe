// 在页面里执行一段表达式并把结果打回来（调试用）
// 用法: node cdp_eval.js <url> <表达式> [等待毫秒]
const url = process.argv[2];
const expr = process.argv[3];
const wait = Number(process.argv[4] || 5000);
const PORT = process.env.CDP_PORT || "9333";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const tab = await (await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" })).json();
  const ws = new WebSocket(tab.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  const errors = [];
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); return; }
    if (m.method === "Runtime.exceptionThrown") {
      errors.push(m.params.exceptionDetails.text + " " + (m.params.exceptionDetails.exception?.description || ""));
    }
    if (m.method === "Runtime.consoleAPICalled") {
      errors.push("[console." + m.params.type + "] " + m.params.args.map((a) => a.value ?? a.description).join(" "));
    }
  };
  const send = (method, params = {}) => new Promise((r) => { const mid = ++id; pending.set(mid, r); ws.send(JSON.stringify({ id: mid, method, params })); });

  await send("Runtime.enable");
  await send("Page.enable");
  await send("Page.navigate", { url });
  await sleep(wait);

  const res = await send("Runtime.evaluate", {
    expression: `(async () => { try { return JSON.stringify(await (${expr})); } catch (e) { return "THROW " + e.message; } })()`,
    awaitPromise: true,
    returnByValue: true,
  });
  console.log("RESULT " + (res.result?.value ?? JSON.stringify(res)));
  if (errors.length) console.log("EVENTS " + JSON.stringify(errors.slice(0, 8)));
  ws.close();
})().catch((e) => { console.error("FAIL", e.message); process.exit(1); });
