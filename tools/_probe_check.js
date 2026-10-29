// 订阅测速页自检：打开页面 → 载入指定记录 → 读关键状态 → 截图
// 用法: node _probe_check.js <url> <out.png> <runId> [等待毫秒]
const url = process.argv[2];
const out = process.argv[3];
const runId = Number(process.argv[4] || 0);
const wait = Number(process.argv[5] || 7000);
const PORT = process.env.CDP_PORT || "9333";
const fs = require("fs");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const tab = await (await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`,
    { method: "PUT" })).json();
  const ws = new WebSocket(tab.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  const errors = [];
  const send = (m, p = {}) => new Promise((res) => {
    const mid = ++id;
    pending.set(mid, res);
    ws.send(JSON.stringify({ id: mid, method: m, params: p }));
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
      errors.push("console: " + m.params.args.map((a) => a.value ?? a.description).join(" "));
    }
  };
  await send("Runtime.enable");
  await send("Page.enable");
  await send("Page.bringToFront");
  await send("Emulation.setDeviceMetricsOverride",
    { width: 1280, height: 1150, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url });
  await sleep(wait);

  const ev = async (expr) => {
    const r = await send("Runtime.evaluate",
      { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) {
      return "ERR " + r.exceptionDetails.text + " " +
        (r.exceptionDetails.exception?.description || "");
    }
    return r.result.value;
  };

  if (runId) {
    await ev(`(async()=>{await loadRun(${runId});document.getElementById('btnToggleLog').click();
             await new Promise(r=>setTimeout(r,900));})()`);
    await sleep(1500);
  }

  const diag = await ev(`(function(){
    const rows = document.querySelectorAll('#tbl tbody tr');
    const runs = document.querySelectorAll('#runs .run');
    const first = rows[0];
    return JSON.stringify({
      title: document.title,
      rows: rows.length,
      runs: runs.length,
      cols: first ? first.children.length : 0,
      headCols: document.querySelectorAll('#tbl thead th').length,
      hasSel: !!document.querySelector('#tbl thead th.sel'),
      firstSpeed: first ? (first.children[6]||{}).textContent.trim() : null,
      nodeCount: document.getElementById('nodeCount').textContent,
      runCount: document.getElementById('runCount').textContent,
      detail: document.getElementById('pDetail').textContent,
      logShown: document.getElementById('logBox').classList.contains('show'),
      logLines: document.getElementById('logBox').childElementCount,
      urlVal: (document.getElementById('fUrl').value||'').slice(0,50)
    });
  })()`);
  console.log("DIAG", diag);

  // 勾选前两行，看操作条是否浮出
  const sel = await ev(`(function(){
    const bs=[...document.querySelectorAll('#tbl tbody input[type=checkbox]')].slice(0,2);
    bs.forEach(b=>{b.checked=true;b.onchange({target:b})});
    return JSON.stringify({bar:document.getElementById('selBar').className,
                           count:document.getElementById('selCount').textContent});
  })()`);
  console.log("SEL", sel);

  const shot = await send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync(out, Buffer.from(shot.data, "base64"));
  console.log("SHOT", out);
  console.log("ERRORS", JSON.stringify(errors));
  process.exit(0);
})().catch((e) => { console.error("FAIL", e); process.exit(1); });
