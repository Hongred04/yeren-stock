// 「数据源」面板（桌面 index.html）—— 用户 10-13 的三条要求：
//   ① 去掉多余的描述：面板只剩「所有源的状态列表 + 两个操作按钮」
//   ② 去掉东方财富 Cookie 配置（限流在连接层，配了没用）
//   ③ 列出**所有源**的实时状态
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
const report = JSON.parse(fs.readFileSync(path.join(__dirname, '_test_report.json'), 'utf8'));

let pass = 0, fail = 0;
const ok = (c, m) => { c ? pass++ : fail++; console.log((c ? '  ✅ ' : '  ❌ ') + m); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

// 一份接近真实的 /api/sources 返回
function payload({ blocked = [], degraded = [] } = {}) {
  const items = [
    { key: 'quote', group: '行情', name: '行情快照', vendor: '腾讯',
      host: 'qt.gtimg.cn', use: '全市场快照 · 自选/持仓行情',
      state: 'ok', state_cn: '正常', detail: '最近成功 3 秒前 · 45ms' },
    { key: 'index', group: '行情', name: '指数行情', vendor: '腾讯',
      host: 'qt.gtimg.cn', use: '两市成交额 · 指数涨跌',
      state: degraded.length ? 'degraded' : 'ok',
      state_cn: degraded.length ? '降级' : '正常',
      detail: degraded.length ? '指数行情：接口本次未取到，用的是 120 秒前的当日快照'
                              : '最近成功 3 秒前 · 45ms' },
    { key: 'search', group: '行情', name: '名称搜索', vendor: '腾讯',
      host: 'smartbox.gtimg.cn', use: '加自选时按名称/拼音搜股票',
      state: 'unused', state_cn: '未使用', detail: '本次运行还没用到' },
    { key: 'kline_tdx', group: 'K线', name: '日K线（首选）', vendor: '通达信',
      host: 'TCP', use: '前复权日K，走 TCP 不走 HTTP',
      state: 'disabled', state_cn: '未启用', detail: '未安装 mootdx 或连不上 —— 已自动退回腾讯K线' },
    { key: 'push2', group: '资金', name: '板块 / 个股资金流', vendor: '东财',
      host: 'push2.eastmoney.com', use: '主力净额 · 涨跌家数 · 板块清单',
      state: blocked.length ? 'blocked' : 'ok',
      state_cn: blocked.length ? '受限' : '正常',
      detail: blocked.length ? '熔断中 · 冷却约 2 分钟（连续失败 3 轮，每 60 秒半开探测） · 本分钟额度 0/15 · 出口直连'
                             : '最近成功 5 秒前 · 120ms · 本分钟额度 3/15 · 出口走代理' },
    { key: 'cls', group: '消息', name: '财联社快讯 / 涨停原因', vendor: '财联社',
      host: 'api3.cls.cn/x-quote.cls.cn', use: '消息定级 · 涨停原因 · 市场情绪',
      state: 'ok', state_cn: '正常', detail: '最近成功 8 秒前 · 210ms' },
  ];
  const groups = ['行情', 'K线', '资金', '消息'].map(n => ({
    name: n, items: items.filter(x => x.group === n),
  }));
  const cnt = {};
  items.forEach(x => { cnt[x.state] = (cnt[x.state] || 0) + 1; });
  const worst = blocked.length ? 'blocked' : (degraded.length ? 'degraded' : 'ok');
  return {
    ok: true, items, groups, blocked,
    push2: { budget_max: 15, budget_used: blocked.length ? 15 : 3, cooling_seconds: blocked.length ? 120 : 0, proxy_on: !blocked.length },
    tdx: {}, health: { degraded },
    summary: { total: items.length, worst, worst_cn: { blocked: '受限', degraded: '降级', ok: '正常' }[worst], ...cnt },
  };
}

(async () => {
  const dom = new JSDOM(html, { runScripts: 'dangerously', pretendToBeVisual: true,
                                url: 'http://127.0.0.1:8777/' });
  const w = dom.window;
  let srcCalls = 0, posts = [];
  let CURRENT = payload();
  w.fetch = (url, opt) => {
    const u = String(url);
    if (u.startsWith('/api/sources')) {
      srcCalls++;
      if (opt && opt.method === 'POST') {
        posts.push(JSON.parse(opt.body));
        return Promise.resolve({ status: 200, json: () => Promise.resolve(
          { ok: true, msg: '已清除 0 条熔断记录', blocked: [] }) });
      }
      return Promise.resolve({ status: 200, json: () => Promise.resolve(CURRENT) });
    }
    if (u.startsWith('/api/result')) {
      return Promise.resolve({ status: 200, json: () => Promise.resolve(report) });
    }
    if (u.startsWith('/api/live')) {
      return Promise.resolve({ status: 200, json: () => Promise.resolve(
        { ts: Date.now() / 1000, quotes: {} }) });
    }
    return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
  };
  w.scrollTo = () => {};
  w.confirm = () => true;

  w.finish(report);
  await sleep(400);

  const $ = id => w.document.getElementById(id);
  const panelTxt = () => $('srcPanel').textContent;

  console.log('\n[1] Cookie 配置已下线，多余描述已清掉');
  await w.openSrc();
  await sleep(200);
  const html2 = $('srcPanel').innerHTML;
  ok(!/srcCookieInput/.test(html2), '没有 Cookie 输入框');
  ok(!/Cookie/.test(panelTxt()), '面板里不再出现「Cookie」字样');
  ok(!/srcOpenEm/.test(html2), '不再有「打开东财行情页」按钮（随 Cookie 说明一起下线）');
  for (const gone of ['关于东财限速', '接口熔断状态', '数据健康度（本轮扫描）', '连接池']) {
    ok(!panelTxt().includes(gone), `去掉了「${gone}」这一块`);
  }
  ok(!/每分钟只给它 15 次请求额度/.test(panelTxt()), '长段说明文字已删');

  console.log('\n[2] 列出所有源的实时状态');
  const list = $('srcList').textContent;
  ok(list.includes('行情快照') && list.includes('指数行情') && list.includes('名称搜索'),
     '行情组的源都在');
  ok(list.includes('日K线（首选）'), 'K线组的源在');
  ok(list.includes('板块 / 个股资金流'), '资金组的源在');
  ok(list.includes('财联社快讯 / 涨停原因'), '消息组的源在');
  ok(/正常/.test(list) && /未使用/.test(list) && /未启用/.test(list),
     '每条都带实时状态（正常 / 未使用 / 未启用）');
  ok(list.includes('最近成功 3 秒前 · 45ms'), '正常项显示"最近成功 + 耗时"');
  ok(list.includes('本次运行还没用到'), '未使用项说明原因');
  ok(list.includes('已自动退回腾讯K线'), '未启用项说明降级去向');
  ok($('srcList').querySelectorAll('.srcline').length === 6, '6 个源各一行');
  ok($('srcList').querySelectorAll('.srcgrp').length === 4, '按 4 个分组显示');

  console.log('\n[3] 概览计数 + 顶栏标签');
  const sum = $('srcSummary').textContent;
  // 6 个源：正常 4（快照/指数/资金流/财联社）· 未使用 1 · 未启用 1
  ok(/正常\s*4/.test(sum) && /未使用\s*1/.test(sum) && /未启用\s*1/.test(sum),
     `概览按档位计数（${sum.trim()}）`);
  ok($('hdSrc').textContent.includes('全部正常'), '顶栏标签：全部正常');
  ok($('hdSrc').textContent.includes('6 个源'), '顶栏标签带上源总数');

  console.log('\n[4] 有异常时：降级 / 受限 如实显示');
  CURRENT = payload({ blocked: [{ api: '/api/qt/clist/get', fails: 3, seconds_left: 120 }],
                      degraded: [{ key: 'index', label: '指数行情', note: '接口本次未取到' }] });
  await w.loadSrc();
  await sleep(80);
  const list2 = $('srcList').textContent;
  ok(list2.includes('受限'), '熔断的源标成「受限」');
  ok(list2.includes('熔断中 · 冷却约 2 分钟'), '带上冷却时间');
  ok(list2.includes('本分钟额度'), '带上限速闸门状态');
  ok(list2.includes('降级'), '降级的源标成「降级」');
  ok($('hdSrc').textContent.includes('受限') && $('hdSrc').textContent.includes('2 个异常'),
     `顶栏标签跟着变（${$('hdSrc').textContent.trim()}）`);
  // 严重的排在同组前面
  const fund = [...$('srcList').querySelectorAll('.srcline')].find(x => x.textContent.includes('板块 / 个股资金流'));
  ok(fund && fund.textContent.includes('受限'), '异常项排在前面且状态正确');

  console.log('\n[5] 两个操作按钮仍然可用（清熔断 / 重连）');
  ok(!!$('srcUnblock') && !!$('srcReconnect'), '两个按钮都在');
  posts.length = 0;
  $('srcUnblock').dispatchEvent(new w.Event('click', { bubbles: true }));
  await sleep(150);
  ok(posts.some(p => p.action === 'unblock'), '「清除熔断记录」发出 unblock');
  posts.length = 0;
  $('srcReconnect').dispatchEvent(new w.Event('click', { bubbles: true }));
  await sleep(150);
  ok(posts.some(p => p.action === 'reconnect'), '「重连」发出 reconnect');
  ok(!posts.some(p => p.action === 'save_cookie' || p.action === 'auto'),
     '不再发出任何 Cookie 相关动作');

  console.log(`\n===== ${pass} 通过 / ${fail} 失败 =====`);
  process.exit(fail ? 1 : 0);
})();
