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

// /api/sources 的返回形状（v1.29.0）：所有源逐条 + 分组 + 概览。
// 状态取自后端"最近一次真实使用"的记录，前端只负责画。
function srcPayload({ blocked = [], push2 = {} } = {}) {
  const down = blocked.length > 0;
  const items = [
    { key: 'quote', group: '行情', name: '行情快照', vendor: '腾讯',
      host: 'qt.gtimg.cn', use: '全市场快照 · 自选/持仓行情',
      state: 'ok', state_cn: '正常', detail: '最近成功 3 秒前 · 45ms' },
    { key: 'push2', group: '资金', name: '板块 / 个股资金流', vendor: '东财',
      host: 'push2.eastmoney.com', use: '主力净额 · 涨跌家数',
      state: down ? 'blocked' : 'ok', state_cn: down ? '受限' : '正常',
      detail: down ? '熔断中 · 冷却约 2 分钟（连续失败 3 轮）' : '最近成功 5 秒前 · 120ms' },
  ];
  return {
    ok: true, blocked, push2, tdx: {}, health: { degraded: [] }, items,
    groups: [{ name: '行情', items: [items[0]] }, { name: '资金', items: [items[1]] }],
    summary: { total: 2, worst: down ? 'blocked' : 'ok',
               worst_cn: down ? '受限' : '正常',
               ok: down ? 1 : 2, blocked: blocked.length },
  };
}

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

  // 自选：输入框已从「只填代码」改成「名称 或 代码」（用户 10-13：搜名字加自选），
  // 代码方式保留 —— 输 6 位数字仍会被就地识别成带前缀代码。
  w.openWatchForm();
  ok(!!$('fQuery'), '加自选表单是「名称 或 代码」输入框');
  ok(!$('fCode'), '旧的「只填代码」输入框已移除');
  $('fQuery').value = 'sz000001';
  await w.submitWatch();
  await new Promise(r => setTimeout(r, 50));
  ok(w.__lastPost && w.__lastPost.action === 'watch_add'
     && w.__lastPost.code === 'sz000001', '提交自选：按代码添加（action/code 正确）');

  // ⑥ 加载态必须收掉（用户实测：进 App 卡在「正在准备…」半屏白，
  //    根因是缓存路径不经过 refresh()，render 里忘了隐藏 loading）
  ok($('loading').style.display === 'none', '有数据后加载态已隐藏');

  // ⑩ 刷新语义：主动刷新必须真的重扫（用户实测「点刷新页面完全不变」）
  //    根因：/api/result 有 3 分钟内存缓存（RESULT_TTL=180s），不清缓存时
  //    render() 用同一份对象重画 → 看起来没刷新。
  //    放在最前面：主动刷新有 60 秒节流，后面的用例会消费掉这个额度。
  const calls = [];
  w.fetch = async (u, opt) => {
    const url = String(u);
    calls.push(((opt && opt.method) || 'GET') + ' ' + url.replace(/^.*\/api/, '/api').split('?')[0]);
    if (url.includes('/api/status')) return { ok: true, status: 200, json: async () => ({ done: true, pct: 100 }) };
    if (url.includes('/api/sources')) return { ok: true, status: 200, json: async () => srcPayload() };
    if (url.includes('/api/invalidate')) return { ok: true, status: 200, json: async () => ({ ok: true }) };
    if (url.includes('/api/start')) return { ok: true, status: 200, json: async () => ({ ok: true }) };
    return { ok: true, status: 200, json: async () => report };
  };
  calls.length = 0;
  await w.refresh(true);
  await new Promise(r => setTimeout(r, 60));
  ok(calls.some(c => c === 'POST /api/invalidate'), '主动刷新先清结果缓存');
  ok(calls.some(c => c === 'GET /api/start'), '主动刷新触发全量重扫');
  ok(calls.indexOf('POST /api/invalidate') < calls.indexOf('GET /api/result'),
     '清缓存发生在取结果之前（顺序不能反）');
  w.stopRescan();                        // 收掉后台重扫轮询，别干扰后续用例

  calls.length = 0;                      // 60 秒节流：下拉手势容易误触
  await w.refresh(true);
  await new Promise(r => setTimeout(r, 60));
  ok(!calls.some(c => c === 'POST /api/invalidate'), '60 秒内重复主动刷新不再重扫（防误触）');

  calls.length = 0;                      // 静默刷新（每分钟）不该触发重扫
  await w.refresh(false);
  await new Promise(r => setTimeout(r, 60));
  ok(!calls.some(c => c === 'POST /api/invalidate') && !calls.some(c => c === 'GET /api/start'),
     '静默刷新只重取结果，不重扫');

  // ⑦ 进度字段名用服务端的 progress（曾经错写成 msg，文案一直卡在"扫描中"）
  const mobSrc = html;
  ok(mobSrc.includes('st.progress') && !mobSrc.includes("st.msg"),
     '进度字段名为 progress（与 /api/status 一致）');

  // ⑧ 东财受限 → 引导去网页做滑块验证（可操作，不是一条死提示）
  let confirmCalls = 0, openedUrl = '';
  w.confirm = () => { confirmCalls++; return false; };   // 记录弹窗，但不真的跳转
  w.open = (u) => { openedUrl = String(u); return {}; };
  w.fetch = async (u) => {
    const url = String(u);
    if (url.includes('/api/sources')) {
      return { ok: true, status: 200, json: async () => srcPayload({
        blocked: [{ api: 'push2.eastmoney.com/ulist.np', fails: 3, seconds_left: 120 }],
        push2: { cooling_seconds: 120, budget_skipped: 5, budget_max: 15, budget_used: 15 },
      }) };
    }
    return { ok: true, status: 200, json: async () => report };
  };
  await w.refresh(true);
  await new Promise(r => setTimeout(r, 80));

  const bar = $('srcBar');
  ok(!!bar && bar.textContent.includes('东财') && bar.textContent.includes('受限'),
     '东财受限时显示提醒条');
  const warnEl = bar && bar.querySelector('.srcwarn');
  ok(!!warnEl, '提醒条挂在 #srcBar（独立于 Tab，缓存路径也能提示）');
  const warnTxt = warnEl ? warnEl.textContent : '';
  ok(!/冷却|让出|ulist|\.com/.test(warnTxt),
     '提醒条不摆技术细节（冷却秒数/接口名/让出次数）');
  ok(/打开东财网页/.test(warnTxt) && /我已验证/.test(warnTxt),
     '提醒条给出「打开东财验证」「我已验证重试」两个入口');
  ok(confirmCalls >= 1, '检测到受限会弹窗申请打开东财网页');
  w.openEmPage();
  ok(/quote\.eastmoney\.com/.test(openedUrl),
     '「打开东财网页」指向东财行情页：' + openedUrl);

  // 同一轮受限不重复打扰（1 小时节流）—— 否则每次轮询都弹窗
  const before = confirmCalls;
  await w.fetchSources();
  ok(confirmCalls === before, '同一轮受限不会反复弹窗（节流生效）');

  // 抽屉里同样有入口，并**列出所有源的实时状态**（v1.29.0：
  // 东财 Cookie 配置已整体下线 —— 限流发生在连接层，配了也没用）
  await w.openSrcSheet();
  const sheetTxt = $('sheetBody').textContent;
  ok($('sheet').classList.contains('on') && sheetTxt.includes('打开东财网页'),
     '数据源抽屉可打开，含东财去验证入口');
  ok(sheetTxt.includes('行情快照') && sheetTxt.includes('板块 / 个股资金流'),
     '抽屉里列出所有数据源');
  ok(sheetTxt.includes('正常') && sheetTxt.includes('受限'), '每条带实时状态');
  ok(!sheetTxt.includes('Cookie'), '不再有东财 Cookie 配置入口');
  w.closeSheet();

  // 非东财受限（新浪）→ 不给"去东财验证"入口（人工验证只对东财成立）
  w.fetch = async (u) => {
    if (String(u).includes('/api/sources')) {
      return { ok: true, status: 200, json: async () => srcPayload({
        blocked: [{ api: 'vip.stock.finance.sina.com.cn/x', fails: 2, seconds_left: 60 }],
        push2: { cooling_seconds: 0, budget_max: 15, budget_used: 3 },
      }) };
    }
    return { ok: true, status: 200, json: async () => report };
  };
  await w.fetchSources();
  const barTxt2 = $('srcBar').textContent;
  ok(barTxt2.includes('新浪') && !barTxt2.includes('打开东财网页'),
     '只有东财受限才给"去东财验证"入口');

  // 一切正常 → 提醒条不占地方
  w.fetch = async (u) => (String(u).includes('/api/sources')
    ? { ok: true, status: 200, json: async () => ({
        blocked: [], push2: { cooling_seconds: 0, budget_max: 15, budget_used: 2 }, cookie: {} }) }
    : { ok: true, status: 200, json: async () => report });
  await w.fetchSources();
  ok($('srcBar').textContent.trim() === '', '数据源正常时不显示提醒条');

  // ⑨ 数值格式化：非有限数不许漏成 "—%"（pctTxt 曾把 NaN 拼成 "—%"）
  const probe = JSON.parse(JSON.stringify(report));
  probe.picks[0].pct = 'abc';          // 非数字 → 必须显示 '—'
  probe.picks[0].turnover = null;
  w.fetch = async (u) => ({ ok: true, status: 200,
    json: async () => (String(u).includes('/api/sources') ? {} : probe) });
  await w.refresh(true);
  await new Promise(r => setTimeout(r, 60));
  const picksTxt = $('tab-picks').textContent;
  ok(!/—%/.test(picksTxt), '非数字涨跌幅不会渲染成 "—%"');
  ok(!/Infinity|NaN/.test(picksTxt), '候选列表不出现 Infinity/NaN');

  // 全局无 NaN / undefined
  const allTxt = $('tab-today').textContent + $('tab-theme').textContent
    + $('tab-picks').textContent + $('tab-hold').textContent;
  ok(!/NaN|Infinity/.test(allTxt), '四个 Tab 均无 NaN/Infinity');

  // ③ 缓存写入（供下次秒开）
  ok(!!w.localStorage.getItem('yeren_mobile'), '报告已写入本地缓存');

  console.log(`\n===== ${pass} 通过 / ${fail} 失败 =====`);
  process.exit(fail ? 1 : 0);
})();
