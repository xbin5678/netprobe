// 一次性自检脚本：打开页面 → 切到「可用节点」→ 全选 → 读状态 → 截图
// 用法: node _ui_check.js <url> <out.png>
const url = process.argv[2] || "http://127.0.0.1:8899/";
const out = process.argv[3] || "E:/公开节点/.tmp/ui.png";
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
  const send = (method, params = {}) => new Promise((res) => {
    const mid = ++id;
    pending.set(mid, res);
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
      errors.push("console: " + m.params.args.map((a) => a.value ?? a.description).join(" "));
    }
  };

  await send("Runtime.enable");
  await send("Page.enable");
  await send("Page.bringToFront");
  await send("Emulation.setDeviceMetricsOverride",
    { width: 1280, height: 1000, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url });
  await sleep(Number(process.argv[4] || 7000));

  const ev = async (expr) => {
    const r = await send("Runtime.evaluate",
      { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) return "ERR " + r.exceptionDetails.text;
    return r.result.value;
  };

  // 切到可用节点标签
  await ev(`document.querySelector('.tab[data-tab="node"]').click()`);
  await sleep(2500);

  const diag = await ev(`(function(){
    const tb = document.querySelector('#nodeTable tbody');
    const rows = tb ? tb.querySelectorAll('tr') : [];
    const first = rows[0];
    return {
      rows: rows.length,
      cols: first ? first.children.length : 0,
      headCols: document.querySelectorAll('#nodeTable thead th').length,
      hasSelCol: !!document.querySelector('#nodeTable thead th.sel'),
      hasTcpCol: [...document.querySelectorAll('#nodeTable thead th')]
                    .some(th => th.textContent.trim() === 'TCP'),
      firstTcp: first ? (first.children[5] ? first.children[5].textContent.trim() : null) : null,
      selBarHidden: document.getElementById('selBar').style.display === 'none'
    };
  })()`);
  console.log("DIAG1", JSON.stringify(diag));

  // 点全选，看 selbar 与计数
  const sel = await ev(`(function(){
    const all = document.getElementById('selAll');
    all.checked = true;
    all.onchange({ target: all });
    return {
      count: document.getElementById('selCount').textContent,
      barShown: document.getElementById('selBar').style.display,
      checked: document.querySelectorAll('#nodeTable tbody input[type=checkbox]:checked').length
    };
  })()`);
  console.log("DIAG2", JSON.stringify(sel));

  // 取消选择
  const clr = await ev(`(function(){
    document.getElementById('btnSelClear').click();
    return { bar: document.getElementById('selBar').style.display,
             selSize: (typeof S !== 'undefined') ? S.sel.size : -1 };
  })()`);
  console.log("DIAG3", JSON.stringify(clr));

  // 只勾前 3 个
  const pick = await ev(`(function(){
    const bs = [...document.querySelectorAll('#nodeTable tbody input[type=checkbox]')].slice(0,3);
    bs.forEach(b => { b.checked = true; b.onchange(); });
    return { count: document.getElementById('selCount').textContent,
             bar: document.getElementById('selBar').style.display };
  })()`);
  console.log("DIAG4", JSON.stringify(pick));

  // 截图
  const shot = await send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync(out, Buffer.from(shot.data, "base64"));
  console.log("SHOT", out);
  console.log("ERRORS", JSON.stringify(errors));
  process.exit(0);
})().catch((e) => { console.error("FAIL", e); process.exit(1); });
