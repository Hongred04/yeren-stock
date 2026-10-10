// 移动端页面（mobile.html）冒烟测试 —— Bottom Tab 版
// 断言：四个 Tab 渲染与切换；结论/情绪/主线/候选/持仓数据正确；抽屉开关；无 NaN
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
  w.fetch = async (u) => {
    const url = String(u);
    let body = {};
    if (url.includes('/api/result')) body = report;
    else if (url.includes('/api/status')) body = { done: true, pct: 100, msg: '完成' };
    return { ok: true, status: 200, json: async () => body };
  };
  w.scrollTo = () => {};

  await sleep(5000);

  const $ = id => w.document.getElementById(id);
  const app = $('app');
  ok(app && app.style.display === 'block', '主容器渲染出来了');

  // ① 今日 Tab（默认显示）
  const t1 = $('tab-today').textContent;
  ok(/今天不要买|可以出手|可以小仓位试水|空仓等启动/.test(t1), '今日 Tab：结论卡有标题');
  ok(t1.includes('情绪温度') && t1.includes('空间板'), '今日 Tab：情绪三格在');
  const temp = (report.meter || {}).temperature;
  if (temp != null) ok(t1.includes(temp + '°'), '温度值正确显示 ' + temp + '°');
  const sp = (report.meter || {}).space || {};
  if (sp.today != null) ok(t1.includes(sp.today + '板'), '空间板值显示 ' + sp.today + '板');
  const out = report.outlook || {};
  if (out.headline) ok(t1.includes(out.headline.slice(0, 8)), '今日 Tab：明日判断在');

  // ② Tab 切换
  ok($('tabbar').querySelectorAll('button').length === 4, '底部导航有 4 个 Tab');
  w.switchTab('theme');
  ok($('tab-theme').classList.contains('on') && !$('tab-today').classList.contains('on'),
     '切到「主线」Tab，旧 Tab 隐藏');
  const ml = report.mainline || {};
  if (ml.theme) ok($('tab-theme').textContent.includes(ml.theme), '主线 Tab：题材「' + ml.theme + '」在');
  if ((ml.signals || []).length) ok($('tab-theme').textContent.includes('五信号'), '主线 Tab：五信号体检在');

  w.switchTab('picks');
  const picks = report.picks || [];
  ok($('tab-picks').classList.contains('on'), '切到「选股」Tab');
  ok(w.document.querySelectorAll('#tab-picks .pick').length === picks.length,
     `候选卡数量 = ${picks.length}`);
  if (picks.length) {
    const dp = $('dotPicks');
    ok(dp && dp.style.display !== 'none', '选股 Tab 角标显示数量');
    w.openSheet(0);
    ok($('sheet').classList.contains('on'), '抽屉能打开');
    const st = $('sheetBody').textContent;
    ok(st.includes(picks[0].name || ''), '抽屉里有第一只候选');
    ok(!/undefined|NaN/.test(st), '抽屉内容无 undefined/NaN');
    w.closeSheet();
    ok(!$('sheet').classList.contains('on'), '抽屉能关闭');
  }

  w.switchTab('hold');
  ok($('tab-hold').classList.contains('on'), '切到「持仓」Tab');
  const holds = (report.portfolio || {}).holdings || [];
  if (holds.length) {
    ok(w.document.querySelectorAll('#tab-hold .hold').length === holds.length,
       `持仓卡数量 = ${holds.length}`);
  } else {
    ok($('tab-hold').textContent.includes('还没有持仓记录'), '无持仓时给引导文案');
  }

  // 全局无 NaN / undefined
  const allTxt = $('tab-today').textContent + $('tab-theme').textContent
    + $('tab-picks').textContent + $('tab-hold').textContent;
  ok(!/NaN|Infinity/.test(allTxt), '四个 Tab 均无 NaN/Infinity');

  // ③ 缓存写入（供下次秒开）
  ok(!!w.localStorage.getItem('yeren_mobile'), '报告已写入本地缓存');

  console.log(`\n===== ${pass} 通过 / ${fail} 失败 =====`);
  process.exit(fail ? 1 : 0);
})();
