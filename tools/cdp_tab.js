// 点开某个标签页再截图：node cdp_tab.js <url> <tab名> <输出png> [等待毫秒]
const url = process.argv[2];
const tab = process.argv[3];
const out = process.argv[4];
const wait = Number(process.argv[5] || 3000);
const PORT = process.env.CDP_PORT || "9333";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const t = await (await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`,
    { method: "PUT" })).json();
  const ws = new WebSocket(t.webSocketDebuggerUrl);
  let id = 0;
  const pend = new Map();
  const errs = [];
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); }
    if (m.method === "Runtime.exceptionThrown")
      errs.push(m.params.exceptionDetails.text + " " +
        (m.params.exceptionDetails.exception?.description || ""));
    if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error")
      errs.push(m.params.args.map((a) => a.value).join(" "));
  };
  const send = (method, params = {}) => new Promise((res) => {
    const i = ++id;
    pend.set(i, res);
    ws.send(JSON.stringify({ id: i, method, params }));
  });
  await new Promise((r) => (ws.onopen = r));
  await send("Runtime.enable");
  await send("Page.enable");
  await send("Emulation.setDeviceMetricsOverride",
    { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url });
  await sleep(2500);
  await send("Runtime.evaluate", {
    expression: `document.querySelector('.tab[data-tab="${tab}"]').click()`,
  });
  await sleep(wait);
  const r = await send("Runtime.evaluate", {
    expression: `JSON.stringify({
      title: document.title,
      active: (document.querySelector('.panel.active')||{}).id,
      state: (document.getElementById('schedState')||{}).textContent,
      next: (document.getElementById('schedNext')||{}).textContent,
      last: (document.getElementById('schedLast')||{}).textContent,
      timer: (document.getElementById('schedTimer')||{}).textContent,
      note: (document.getElementById('schedNote')||{}).textContent,
      modeOn: (document.querySelector('#segSchedMode .on')||{}).textContent,
      at: (document.getElementById('schedAt')||{}).value,
      every: (document.getElementById('schedEvery')||{}).value,
      checked: (document.getElementById('schedOn')||{}).checked
    })`, returnByValue: true,
  });
  console.log("页面:", r.result.result.value);
  if (errs.length) console.log("JS 报错:", errs.join("\n"));
  const shot = await send("Page.captureScreenshot", { format: "png" });
  require("fs").writeFileSync(out, Buffer.from(shot.result.data, "base64"));
  console.log("已截图", out);
  ws.close();
  process.exit(0);
})();
