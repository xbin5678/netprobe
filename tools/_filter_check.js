// 订阅测速页「筛选」自检：全部 → 有下载速度 → 可用 → 速度≥N → 搜索 → 测这些
// 用法: node _filter_check.js <url> <out.png> <runId> [等待毫秒]
const url = process.argv[2];
const out = process.argv[3];
const runId = Number(process.argv[4] || 0);
const wait = Number(process.argv[5] || 6000);
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
    { width: 1280, height: 1200, deviceScaleFactor: 1, mobile: false });
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
    await ev(`(async()=>{await loadRun(${runId});})()`);
    await sleep(700);
  }

  const snap = `(function(){
    const rows=[...document.querySelectorAll('#tbl tbody tr')];
    const cells=(r)=>[...r.children].map(c=>c.textContent.trim());
    return JSON.stringify({
      rows: rows.length,
      count: document.getElementById('nodeCount').textContent,
      fcount: document.getElementById('fCount').textContent,
      cSpeed: document.getElementById('cSpeed').textContent,
      cOk: document.getElementById('cOk').textContent,
      emptyShown: document.getElementById('empty').classList.contains('show'),
      emptyText: document.getElementById('empty').textContent.slice(0,40),
      tab: (document.querySelector('#segFilter button.on')||{}).textContent,
      first: rows.length?cells(rows[0]).slice(1,8):null,
      last: rows.length?cells(rows[rows.length-1]).slice(1,8):null,
      selCount: document.getElementById('selCount').textContent,
      expTip: document.getElementById('expTip').textContent.trim()
    });
  })()`;

  console.log("ALL   ", await ev(snap));

  // 1) 有下载速度
  await ev(`document.querySelector('#segFilter button[data-f="speed"]').click()`);
  await sleep(350);
  console.log("SPEED ", await ev(snap));

  // 2) 速度 ≥ 2 MB/s
  await ev(`(function(){const i=document.getElementById('fMinSpeed');
             i.value='2';i.dispatchEvent(new Event('input'));})()`);
  await sleep(350);
  console.log("MIN2  ", await ev(snap));

  // 3) 全选当前筛选 + 看选中条
  await ev(`(function(){const a=document.getElementById('selAll');
             a.checked=true;a.onchange({target:a});})()`);
  await sleep(250);
  console.log("SELALL", await ev(snap));

  // 4) 切回全部，隐藏的选中项应被清掉
  await ev(`document.getElementById('fMinSpeed').value='0';
            document.getElementById('fMinSpeed').dispatchEvent(new Event('input'));
            document.querySelector('#segFilter button[data-f="all"]').click()`);
  await sleep(350);
  console.log("BACK  ", await ev(snap));

  // 5) 可用
  await ev(`document.querySelector('#segFilter button[data-f="ok"]').click()`);
  await sleep(300);
  console.log("OK    ", await ev(snap));

  // 6) 测速失败
  await ev(`document.querySelector('#segFilter button[data-f="fail"]').click()`);
  await sleep(300);
  console.log("FAIL  ", await ev(snap));

  // 7) 搜索
  await ev(`document.querySelector('#segFilter button[data-f="all"]').click();
            const s=document.getElementById('fSearch');
            s.value='meituan';s.dispatchEvent(new Event('input'));`);
  await sleep(300);
  console.log("SEARCH", await ev(snap));

  // 8) 回到「有下载速度」截图
  await ev(`(function(){const s=document.getElementById('fSearch');
             s.value='';s.dispatchEvent(new Event('input'));
             document.querySelector('#segFilter button[data-f="speed"]').click();})()`);
  await sleep(500);

  const shot = await send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync(out, Buffer.from(shot.data, "base64"));
  console.log("SHOT", out);
  console.log("ERRORS", JSON.stringify(errors));
  process.exit(0);
})().catch((e) => { console.error("FAIL", e); process.exit(1); });
