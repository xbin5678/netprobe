// 订阅页「档位」自检：三档切换 → 读计数/链接 → 看节点表速度列
// 用法: node _sub_check.js <url> <out.png> [等待毫秒]
const url = process.argv[2];
const out = process.argv[3];
const wait = Number(process.argv[4] || 6000);
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
    { width: 1280, height: 1300, deviceScaleFactor: 1, mobile: false });
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

  await ev(`setTab("sub")`);
  await sleep(1200);

  const snap = `(function(){
    const cards=[...document.querySelectorAll('#subList .subcard')];
    return JSON.stringify({
      tab: (document.querySelector('#segSubTier button.on')||{}).textContent,
      tAlive: document.getElementById('tAlive').textContent,
      tSpeed: document.getElementById('tSpeed').textContent,
      tTcp: document.getElementById('tTcp').textContent,
      head: document.getElementById('subAlive').textContent,
      cards: cards.length,
      firstUrl: cards[0]?cards[0].querySelector('.suburl').value:'',
      firstCount: cards[0]?cards[0].querySelector('.cnt').textContent.trim():'',
      note: document.getElementById('subNote').textContent.slice(0,60)
    });
  })()`;

  console.log("ALIVE ", await ev(snap));
  await ev(`document.querySelector('#segSubTier button[data-tier="speed"]').click()`);
  await sleep(1200);
  console.log("SPEED ", await ev(snap));
  await ev(`document.querySelector('#segSubTier button[data-tier="tcp"]').click()`);
  await sleep(1200);
  console.log("TCP   ", await ev(snap));

  // 回 speed 档 + 看节点表的速度列
  await ev(`document.querySelector('#segSubTier button[data-tier="speed"]').click()`);
  await sleep(900);
  await ev(`setTab("node")`);
  await sleep(800);
  await ev(`(function(){const s=document.getElementById('fSpeed');
             s.value='speed';s.onchange();})()`);
  await sleep(1200);
  const tbl = await ev(`(function(){
    const rows=[...document.querySelectorAll('#nodeTable tbody tr')];
    return JSON.stringify({
      rows: rows.length,
      headCols: document.querySelectorAll('#nodeTable thead th').length,
      cells: rows[0]?[...rows[0].children].map(c=>c.textContent.trim()):null,
      head: document.getElementById('nodeHead').textContent
    });
  })()`);
  console.log("NODES ", tbl);

  // 回到订阅页截图
  await ev(`setTab("sub")`);
  await sleep(800);
  await ev(`document.querySelector('#segSubTier button[data-tier="speed"]').click()`);
  await sleep(1200);

  const shot = await send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync(out, Buffer.from(shot.data, "base64"));
  console.log("SHOT", out);
  console.log("ERRORS", JSON.stringify(errors));
  process.exit(0);
})().catch((e) => { console.error("FAIL", e); process.exit(1); });
