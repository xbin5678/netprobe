// 用 CDP 驱动无头浏览器：等页面真正加载完（含 SSE），截图 + 读关键字段 + 收集报错
// 用法: node cdp_shot.js <url> <输出png> [等待毫秒]
const url = process.argv[2];
const out = process.argv[3];
const wait = Number(process.argv[4] || 7000);
const PORT = process.env.CDP_PORT || "9333";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  // 新建标签页（Chrome 111+ 要 PUT）
  const r = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" });
  const tab = await r.json();
  const ws = new WebSocket(tab.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  const errors = [];
  const logs = [];

  const send = (method, params = {}) =>
    new Promise((resolve) => {
      const mid = ++id;
      pending.set(mid, resolve);
      ws.send(JSON.stringify({ id: mid, method, params }));
    });

  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });

  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); return; }
    if (m.method === "Runtime.exceptionThrown") {
      errors.push(m.params.exceptionDetails.text + " " +
        (m.params.exceptionDetails.exception?.description || ""));
    }
    if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") {
      logs.push(m.params.args.map((a) => a.value ?? a.description).join(" "));
    }
    if (m.method === "Log.entryAdded" && m.params.entry.level === "error") {
      errors.push(m.params.entry.text);
    }
  };

  await send("Page.enable");
  await send("Runtime.enable");
  await send("Log.enable");
  await send("Page.bringToFront");   // 后台标签会被 Chrome 限流，切到前台再测
  if (process.env.WIDTH) {
    await send("Emulation.setDeviceMetricsOverride", {
      width: Number(process.env.WIDTH), height: Number(process.env.HEIGHT || 900),
      deviceScaleFactor: 1, mobile: true,
    });
  }
  await send("Page.navigate", { url });
  await sleep(wait);
  // 带 ?token= 会被 302 到 /，hash 会丢，所以切标签要在跳转之后再做
  if (process.env.SETTAB) {
    await send("Runtime.evaluate", { expression: `setTab(${JSON.stringify(process.env.SETTAB)})` });
    await sleep(Number(process.env.SETTAB_WAIT || 2000));
  }

  const probe = await send("Runtime.evaluate", {
    expression: `JSON.stringify({
      title: document.title,
      total: document.querySelector("#sTotal")?.textContent,
      alive: document.querySelector("#sAlive")?.textContent,
      src: document.querySelector("#sSrc")?.textContent,
      phase: document.querySelector("#pPhase")?.textContent,
      rows: document.querySelectorAll("#nodeTable tr").length,
      logLines: document.querySelectorAll("#log div").length,
      sw: document.body.scrollWidth,
      cw: document.documentElement.clientWidth
    })`,
    returnByValue: true,
  });
  console.log("PAGE " + probe.result.value);
  if (errors.length) console.log("ERRORS " + JSON.stringify(errors.slice(0, 5)));
  if (logs.length) console.log("CONSOLE " + JSON.stringify(logs.slice(0, 5)));

  const shot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  if (shot?.data) {
    const fs = require("fs");
    fs.writeFileSync(out, Buffer.from(shot.data, "base64"));
    console.log("SHOT " + out);
  }
  ws.close();
}

main().catch((e) => { console.error("FAIL", e.message); process.exit(1); });
