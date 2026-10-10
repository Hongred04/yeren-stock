// 移动端页面（mobile.html）冒烟测试
// 断言重点：能渲染出结论/情绪/主线/候选/持仓/明日判断；抽屉能打开；NaN 不出现
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'mobile.html'), 'utf8');
const report = JSON.parse(fs.readFileSync(path.join(__dirname, '_test_report.json'), 'utf8'));

let pass = 0, fail = 0;
const ok = (c, m) => { c ? pass++ : fail++; console.log((c ? '  ✅ ' : '  ❌ ') + m); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const dom = new JSDOM(html, {
    runScripts: 'dangerously', pretendToBeVisual: true,
    url: 'http://127.0.0.1:8777/mobile.html',
  });
  const w = dom.window;

  // 全量 mock 后端：/api/result 给夹具；/api/status 直接 done
  w.fetch = async (u) => {
    const url = String(u);
    let body = {};
    if (url.includes('/api/result')) body = report;
    else if (url.includes('/api/status')) body = { done: true, pct: 100, msg: '完成' };
    return { ok: true, status: 200, json: async () => body };
  };
  w.open = () => {};

  // 等轮询链走完（2s 一次）+ 渲染
  await sleep(5000);

  const $ = id => w.document.getElementById(id);
  const app = $('app');
  ok(app && app.style.display === 'block', '主容器渲染出来了');
  const txt = app ? app.textContent : '';

  // ① 结论卡
  ok(/今天不要买|可以出手|可以小仓位试水|空仓等启动/.test(txt), '今日结论卡有标题');
  ok(!/undefined|NaN|Infinity/.test(txt), '页面无 undefined/NaN/Infinity');

  // ② 情绪三格
  ok(txt.includes('情绪温度') && txt.includes('空间板'), '情绪三格（温度/空间板）在');
  const temp = (report.meter || {}).temperature;
  if (temp != null) ok(txt.includes(temp + '°'), '温度值正确显示 ' + temp + '°');
  const sp = (report.meter || {}).space || {};
  if (sp.today != null) ok(txt.includes(sp.today + '板'), '空间板值显示 ' + sp.today + '板');

  // ③ 主线
  const ml = report.mainline || {};
  if (ml.theme) ok(txt.includes(ml.theme), '主线题材「' + ml.theme + '」在');

  // ④ 候选 + 抽屉
  const picks = report.picks || [];
  ok(w.document.querySelectorAll('.pick').length === picks.length,
     `候选卡数量 = ${picks.length}`);
  if (picks.length) {
    w.openSheet(0);
    const sheet = $('sheet');
    ok(sheet.classList.contains('on'), '抽屉能打开');
    const st = $('sheetBody').textContent;
    ok(st.includes(picks[0].name || ''), '抽屉里有第一只候选的名字');
    ok(!/undefined|NaN/.test(st), '抽屉内容无 undefined/NaN');
    w.closeSheet();
    ok(!$('sheet').classList.contains('on'), '抽屉能关闭');
  }

  // ⑤ 持仓（夹具里若有）
  const holds = (report.portfolio || {}).holdings || [];
  if (holds.length) ok(w.document.querySelectorAll('.hold').length === holds.length,
                       `持仓卡数量 = ${holds.length}`);

  // ⑥ 明日判断
  const out = report.outlook || {};
  if (out.headline) ok(txt.includes(out.headline.slice(0, 8)), '明日判断在');

  console.log(`\n===== ${pass} 通过 / ${fail} 失败 =====`);
  process.exit(fail ? 1 : 0);
})();
