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

  // ④ 资金卡 items 渲染（回归：曾把对象当字符串拼出 [object Object]）
  const capItems = (((report.flow || {}).capital || {}).items) || [];
  if (capItems.length) {
    const themeTxt = $('tab-theme').textContent;
    ok(!themeTxt.includes('[object Object]'), '资金卡无 [object Object]（已修）');
    ok(themeTxt.includes(String(capItems[0].k || '')), '资金卡正确显示 k 字段');
  }

  // ⑤ 持仓/自选：添加按钮 + 表单 + 提交
  ok($('tab-hold').textContent.includes('记持仓'), '持仓 Tab 有「＋ 记持仓」');
  ok($('tab-hold').textContent.includes('加自选'), '持仓 Tab 有「＋ 加自选」');
  w.openHoldForm();
  ok($('sheet').classList.contains('on') && !!$('fCode'), '记持仓表单能打开（含代码输入框）');
  ok(!!$('fCost') && !!$('fShares'), '表单含成本/股数输入');

  // 提交（mock POST，断言 payload 正确）
  w.fetch = async (u, opt) => {
    const url = String(u);
    if (url.includes('/api/portfolio') && opt && opt.method === 'POST') {
      w.__lastPost = JSON.parse(opt.body);
      return { ok: true, status: 200,
               json: async () => ({ ok: true, portfolio: report.portfolio }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  };
  $('fCode').value = '600000';            // 不带前缀，测自动归一
  $('fCost').value = '10.5';
  $('fShares').value = '1000';
  await w.submitHold();
  await new Promise(r => setTimeout(r, 50));
  ok(w.__lastPost && w.__lastPost.action === 'add'
     && w.__lastPost.code === 'sh600000', '提交持仓：代码自动归一 sh 前缀');
  ok(w.__lastPost && w.__lastPost.cost === 10.5 && w.__lastPost.shares === 1000,
     '提交持仓：成本/股数解析正确');
  ok(!$('sheet').classList.contains('on'), '提交后抽屉自动关闭');

  // 自选同理（只看 payload）
  w.openWatchForm();
  $('fCode').value = 'sz000001';
  await w.submitWatch();
  await new Promise(r => setTimeout(r, 50));
  ok(w.__lastPost && w.__lastPost.action === 'watch_add'
     && w.__lastPost.code === 'sz000001', '提交自选：action/code 正确');

  // ⑥ 加载态必须收掉（用户实测：进 App 卡在「正在准备…」半屏白，
  //    根因是缓存路径不经过 refresh()，render 里忘了隐藏 loading）
  ok($('loading').style.display === 'none', '有数据后加载态已隐藏');

  // ⑦ 进度字段名用服务端的 progress（曾经错写成 msg，文案一直卡在"扫描中"）
  const mobSrc = html;
  ok(mobSrc.includes('st.progress') && !mobSrc.includes("st.msg"),
     '进度字段名为 progress（与 /api/status 一致）');

  // ⑧ 数据源提醒条：受限时要说人话（不摆冷却秒数/让出次数等技术细节）
  w.fetch = async (u, opt) => {
    const url = String(u);
    if (url.includes('/api/sources')) {
      return { ok: true, status: 200, json: async () => ({
        blocked: [{ api: 'push2.eastmoney.com/ulist.np', fails: 3, seconds_left: 120 }],
        push2: { cooling_seconds: 120, budget_skipped: 5, budget_max: 15, budget_used: 15 },
        cookie: {}, }) };
    }
    return { ok: true, status: 200, json: async () => report };
  };
  await w.refresh(true);
  await new Promise(r => setTimeout(r, 80));
  const todayTxt = $('tab-today').textContent;
  ok(todayTxt.includes('数据源暂时受限'), '数据源受限时显示提醒条');
  ok(todayTxt.includes('东财'), '提醒条指名道姓（东财）');
  const warnEl = w.document.querySelector('#tab-today .srcwarn');
  const warnTxt = warnEl ? warnEl.textContent : '';
  ok(warnEl && !/冷却|让出|push2|ulist|\.com/.test(warnTxt),
     '提醒条只说人话（不含冷却秒数/接口名/让出次数）');

  // 全局无 NaN / undefined
  const allTxt = $('tab-today').textContent + $('tab-theme').textContent
    + $('tab-picks').textContent + $('tab-hold').textContent;
  ok(!/NaN|Infinity/.test(allTxt), '四个 Tab 均无 NaN/Infinity');

  // ③ 缓存写入（供下次秒开）
  ok(!!w.localStorage.getItem('yeren_mobile'), '报告已写入本地缓存');

  console.log(`\n===== ${pass} 通过 / ${fail} 失败 =====`);
  process.exit(fail ? 1 : 0);
})();
