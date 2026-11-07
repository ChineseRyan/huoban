// 尺寸回归：node tools/ui_resize_check.js <url> [url2 ...] [--allow-scroll] [--sizes=720x520,1013x580]
//
// 为什么要有这一支：界面是手写的 CSS，窗口又能拖，所以"某个尺寸下坏掉"是常态。
// 肉眼看只会说"散架了"，量出来才知道坏在哪 —— 上一轮 720×760 那片空白，
// 真相是 #r-warnings 继承了 flex:1 被撑到页底，不是"没居中"。
//
// 每个尺寸查四件事：
//   1. 文档不该溢出（整页出现滚动条 = 布局没算对）
//   2. 滚动容器不该溢出（除非 --allow-scroll：④ 页那种本来就长的列表）
//   3. 地标块不能越出视口（越界 = 被切掉）
//   4. 地标块之间不能重叠（重叠 = 挤在一起）
// 任何一条不满足 → 打印 FAIL 原因，退出码 1。
//
// 浏览器可执行文件：默认按系统常见位置找 Chrome；装在别处（或用别的 Chromium）
// 就设环境变量 CHROME 指过去，例如 CHROME="D:/somewhere/chrome.exe"。
const { spawn } = require("child_process");
const os = require("os");
const path = require("path");
// 浏览器位置因机器而异：优先用环境变量 CHROME，没设就按各平台常见安装位置找。
const CHROME = process.env.CHROME || [
  process.env.PROGRAMFILES && path.join(process.env.PROGRAMFILES, "Google/Chrome/Application/chrome.exe"),
  "/usr/bin/google-chrome",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
].filter(Boolean).find(p => require("fs").existsSync(p)) || "chrome";
const PORT = 9500 + Math.floor(Math.random() * 300);

const args = process.argv.slice(2);
const ALLOW_SCROLL = args.includes("--allow-scroll");
const sizeArg = args.find(a => a.startsWith("--sizes="));
const urls = args.filter(a => !a.startsWith("--"));

// 八个"最容易坏"的尺寸：两档最小、断点两侧、三种常规窗口、一个超宽。
const SIZES = sizeArg
  ? sizeArg.slice(8).split(",").map(s => s.split("x").map(Number))
  : [[720, 520], [720, 760], [810, 520], [830, 520],
     [1013, 580], [1040, 520], [1040, 760], [1600, 900]];

// 地标：每一屏的主体块。找不到的选择器自动跳过，所以一份表管所有屏。
const LANDMARKS = [
  ".r-title", "#r-figures", "#r-warnings", "#r-note", ".blanknote",
  ".prayer-title", ".prayer-line", ".busy-cancel", ".busy-foot",
  ".drop-title", ".drop-hint", ".chrome", ".footnote",
  // ⑤ 元数据页：画框 / 右栏。封面动作条与 facts 都长在 .meta-main 里面，
  // 把它们也列进来只会报"包含"这种假重叠 —— 取两栏的外框就够。
  ".cover-stage", ".meta-main",
  ".done-name", ".done-facts", ".err-title", ".err-body",
  // ④ 复核页：左侧竖目录 / 右侧面板 / 右下角校定。
  // 面板里的小块（标题、批量条、列表）都在 .b-pane 里面，加进来只会
  // 报"包含"这种假重叠，所以只取这两块外框。
  ".b-dir", ".b-pane",
  ".actions",
];

const CHECK = `(function(landmarks, allowScroll){
  var out = { vw: innerWidth, vh: innerHeight, problems: [], notes: [] };
  var d = document.documentElement;
  var docOv = d.scrollHeight - d.clientHeight;
  if (docOv > 1) out.problems.push("整页溢出 " + docOv + "px");

  // 视图靠 body[data-view] 切换，隐藏的 .view 是 display:none —— 用可见性筛，不靠类名。
  var vis = function (e) { return !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length); };
  var sc = null;
  Array.prototype.forEach.call(document.querySelectorAll(".scroll"), function (e) {
    if (vis(e)) sc = e;
  });
  if (sc && !allowScroll) {
    var ov = sc.scrollHeight - sc.clientHeight;
    if (ov > 1) out.problems.push("滚动区溢出 " + ov + "px（不该有滚动条）");
  }

  var boxes = [];
  landmarks.forEach(function (sel) {
    var els = document.querySelectorAll(sel);
    Array.prototype.forEach.call(els, function (e, i) {
      if (!vis(e)) return;
      var r = e.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) return;
      var name = sel + (els.length > 1 ? "[" + i + "]" : "");
      // fixed 元素（右上角主题按钮那类覆盖层）只查越界，不查重叠：
      // 覆盖层的存在就是要浮在版面之上，块级盒子的宽度又总是横贯整栏，
      // 拿盒子判重叠会天天报假警。真正该查的是"字有没有被盖住"，那是另一码事。
      var fixed = getComputedStyle(e).position === "fixed";
      boxes.push({ name: name, t: r.top, b: r.bottom, l: r.left, r: r.right, fixed: fixed });
      var over = [];
      if (r.top < -1) over.push("上 " + Math.round(-r.top));
      if (r.bottom > innerHeight + 1) over.push("下 " + Math.round(r.bottom - innerHeight));
      if (r.left < -1) over.push("左 " + Math.round(-r.left));
      if (r.right > innerWidth + 1) over.push("右 " + Math.round(r.right - innerWidth));
      if (over.length) out.problems.push(name + " 越界（" + over.join("，") + "）");
    });
  });

  for (var i = 0; i < boxes.length; i++) {
    for (var j = i + 1; j < boxes.length; j++) {
      var a = boxes[i], b = boxes[j];
      if (a.fixed || b.fixed) continue;
      if (a.name.split("[")[0] === b.name.split("[")[0]) continue;
      var ox = Math.min(a.r, b.r) - Math.max(a.l, b.l);
      var oy = Math.min(a.b, b.b) - Math.max(a.t, b.t);
      if (ox > 2 && oy > 2) {
        out.problems.push(a.name + " 与 " + b.name + " 重叠 " +
          Math.round(ox) + "×" + Math.round(oy));
      }
    }
  }
  out.landmarks = boxes.length;
  return JSON.stringify(out);
})`;

const sleep = ms => new Promise(r => setTimeout(r, ms));

const child = spawn(CHROME, ["--headless=new", "--disable-gpu", "--no-sandbox",
  "--no-first-run", "--no-default-browser-check", "--hide-scrollbars",
  "--force-device-scale-factor=1", "--remote-debugging-port=" + PORT,
  "--user-data-dir=" + path.join(os.tmpdir(), "huoban-profile-resize"), "about:blank"],
  { stdio: "ignore" });

(async () => {
  let list = null;
  for (let i = 0; i < 80; i++) {
    try { const r = await fetch("http://127.0.0.1:" + PORT + "/json/list"); list = await r.json(); if (list && list.length) break; } catch (e) {}
    await sleep(250);
  }
  if (!list) throw new Error("chrome 没起来");
  const page = list.find(t => t.type === "page");
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let id = 0; const pending = new Map();
  ws.onmessage = ev => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); } };
  const send = (method, params) => new Promise(res => { const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method, params: params || {} })); });

  await send("Page.enable");
  let bad = 0, total = 0;

  for (const url of urls) {
    await send("Emulation.setDeviceMetricsOverride", { width: 1013, height: 580, deviceScaleFactor: 1, mobile: false });
    await send("Page.navigate", { url });
    await sleep(600);
    const short = url.split("/").pop().split("?")[0];
    console.log("\n" + short + (ALLOW_SCROLL ? "（允许滚动）" : ""));

    for (const [w, h] of SIZES) {
      await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile: false });
      await sleep(180);
      const r = await send("Runtime.evaluate", { expression: CHECK + "(" + JSON.stringify(LANDMARKS) + "," + ALLOW_SCROLL + ")", returnByValue: true });
      const o = JSON.parse(r.result.value);
      total++;
      if (o.problems.length) {
        bad++;
        console.log("  FAIL " + w + "×" + h + "  " + o.problems.join(" / "));
      } else {
        console.log("  ok   " + w + "×" + h + "  （" + o.landmarks + " 个地标）");
      }
    }
  }

  console.log("\n" + (bad ? "✗ " + bad + " / " + total + " 个尺寸有问题" : "✓ " + total + " 个尺寸全过"));
  ws.close(); child.kill();
  process.exit(bad ? 1 : 0);
})().catch(e => { console.error(e); child.kill(); process.exit(1); });