// 截图夹具：把 gui/ui.html 包一层假 api，强制切到指定视图，供无头截图。
// 用法：node _shot/build.js <view> [payload.json] [--static]
//   view: drop | busy | result | review | meta | done | error
//   --static 冻住所有过渡（单帧截图必须加，否则拍到动画中间态）
//
// 三段拼接，各是独立文件（不在这儿写 JS 字符串，转义容易出错）：
//   stub_api.js  假 api：review_detail / review_more / preview_cover 走真夹具
//   stub_tail.js 收尾脚本：切视图、灌 payload、切主题、?k= / ?seen=all / ?cv=
//   covers.json / payload-review.json 等由 mkcovers.py、mkrevfix.py 生成
// 原理：ui.html 全部逻辑在 IIFE 里，外部拿不到 setView，所以在 "use strict";
// 之后插一行把内部函数挂到 window.__ui 上。
const fs = require('fs');
const path = require('path');
// 路径一律按脚本自身位置算，换个人 clone 到哪个盘都能跑。
const OUT = __dirname + path.sep;
const SRC = path.join(__dirname, '..', 'gui', 'ui.html');

const raw = process.argv.slice(2);
const STATIC = raw.indexOf('--static') >= 0;
const args = raw.filter(function (a) { return a !== '--static'; });
const view = args[0] || 'drop';
const payloadFile = args[1] || null;

const FN = { result: 'render', review: 'renderReview', meta: 'renderMeta', done: 'renderDone' };

let covers = '[]';
try { covers = fs.readFileSync(OUT + 'covers.json', 'utf8').trim(); } catch (e) {}
let payload = '{}';
if (payloadFile) { payload = fs.readFileSync(payloadFile, 'utf8').trim(); }

let tail = fs.readFileSync(OUT + 'stub_tail.js', 'utf8');
tail = tail.split('@COVERS@').join(covers);
tail = tail.split('@PAYLOAD@').join(payload);
tail = tail.split('@VIEW@').join(JSON.stringify(view));
tail = tail.split('@FN@').join(FN[view] || '');

const stubApi = fs.readFileSync(OUT + 'stub_api.js', 'utf8');
const anchor = '(function () {\n  "use strict";';
// setJob：⑤屏的封面预览要往后端要图，后端靠 job_id 找那一场的清洗结果，
// 夹具直接调 renderMeta 时 currentJob 还是 null，封面一处都出不来。
const HOOK = 'window.__ui = { setView: setView, render: render, renderReview: renderReview, ' +
             'renderMeta: renderMeta, renderDone: renderDone, applyTheme: applyTheme, ' +
             'busyEnter: busyEnter, showError: showError, ' +
             'setJob: function (j) { currentJob = j; } };';

let out = fs.readFileSync(SRC, 'utf8');
if (out.indexOf(anchor) < 0) { throw new Error('IIFE 锚点没找到，ui.html 结构变了'); }
out = out.replace(anchor, anchor + '\n  ' + HOOK);
out = out.replace(/<body([^>]*)>/, function (m) {
  return m + '<script>' + stubApi + '</scr' + 'ipt>' +
    (STATIC ? '<style>* { transition: none !important; }</style>' : '');
});
out = out.replace('</body>', '<scr' + 'ipt>' + tail + '</scr' + 'ipt></body>');
fs.writeFileSync(OUT + 'view-' + view + '.html', out, 'utf8');
console.log('wrote', OUT + 'view-' + view + '.html');
