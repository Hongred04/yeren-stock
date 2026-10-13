// E5「低位承接方向」卡片（index.html）—— 用户 10-13 要求「给我添加进现有体系中」
// 断言：三个闸门全过才出卡；缺哪个闸门就如实说缺哪个；方向带评分/依据/催化/口径/仓位；
// 模块关闭或报告里没这个键都不崩。
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
const base = JSON.parse(fs.readFileSync(path.join(__dirname, '_test_report.json'), 'utf8'));

let pass = 0, fail = 0;
const ok = (c, m) => { c ? pass++ : fail++; console.log((c ? '  ✅ ' : '  ❌ ') + m); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

const CANDS = [
  { name: '黑龙江', pct: -1.17, main: 3.1, main_pct: 5.08, streak: 1, cum: -1.6,
    role: '', catalyst: '', score: 86.1,
    tags: ['价没动·钱在进（-1.17%）', '首日净流入（资金刚动）', '近3日累计 -1.6%（位置偏低）'],
    how: '不追高：等它回踩或横盘再看', size: '试错仓（不超过 5.0 成）—— 退潮期不做重仓' },
  { name: '酿酒概念', pct: 0.01, main: 0.76, main_pct: 2.12, streak: 1, cum: -0.5,
    role: '支线', catalyst: '渠道库存总体仍在去化，部分受访酒商库存同比下降约10%-20%',
    score: 76.5,
    tags: ['价没动·钱在进（+0.01%）', '首日净流入（资金刚动）', '有催化', '支线（非主线 → 小波段定位）'],
    how: '先盯金种子酒能不能续强', size: '试错仓（不超过 5.0 成）' },
];
const ACTIVE = {
  enabled: true, active: true, reason: '',
  gate: {
    ebb: { ok: true, txt: '情绪阶段 ebb（退潮）' },
    stock_game: { ok: true, txt: '量能 峰值量能（存量博弈 → 只做小波段）' },
    inst_out: { ok: true, txt: '机构专用席位净卖 -7.05 亿（在高位兑现 → 钱要找地方去）' },
  },
  candidates: CANDS,
  summary: '机构在高位兑现 -7.05 亿（游资净买 +1.85 亿），退潮期 + 存量博弈 —— 本轮可看：黑龙江、酿酒概念。',
  note: '本模块只回答「退潮期机构兑现的钱可能去哪」，是提醒去看一眼，不是买入指令。',
};
const INACTIVE = {
  enabled: true, active: false,
  gate: {
    ebb: { ok: true, txt: '情绪阶段 ebb（退潮）' },
    stock_game: { ok: false, txt: '量能 量能舒适（有增量 → 属主升场景，不在本模块射程）' },
    inst_out: { ok: true, txt: '机构专用席位净卖 -7.05 亿（在高位兑现）' },
  },
  candidates: [], summary: '', reason: '场景不成立（未同时满足：存量博弈）',
  note: '',
};

function mk(rb) {
  const r = JSON.parse(JSON.stringify(base));
  if (rb === null) { delete r.rebound; return r; }
  r.rebound = rb;
  return r;
}

(async () => {
  const boot = async (payload) => {
    const dom = new JSDOM(html, { runScripts: 'dangerously', pretendToBeVisual: true,
                                  url: 'http://127.0.0.1:8777/' });
    const w = dom.window;
    w.fetch = (u) => {
      const s = String(u);
      if (s.startsWith('/api/has-cache')) return Promise.resolve({ status: 200, json: () => Promise.resolve({ cached: true }) });
      if (s.startsWith('/api/result')) return Promise.resolve({ status: 200, json: () => Promise.resolve(payload) });
      if (s.startsWith('/api/sources')) return Promise.resolve({ status: 200, json: () => Promise.resolve({ ok: true, items: [], groups: [], summary: {}, blocked: [], push2: {}, tdx: {}, health: {} }) });
      return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
    };
    w.scrollTo = () => {};
    await w.load();
    await sleep(400);
    return { w, $: id => w.document.getElementById(id) };
  };

  console.log('\n[1] 三个闸门全过 → 出卡，内容完整');
  let { $ } = await boot(mk(ACTIVE));
  const el = $('rebound');
  ok(!!el && el.textContent.includes('低位承接方向'), '卡片渲染出来了');
  const t = el.textContent;
  ok(t.includes('机构兑现的钱可能去哪'), '标题点明模块要回答的问题');
  for (const g of ['情绪阶段 ebb', '存量博弈', '高位兑现']) ok(t.includes(g), `闸门写着：${g}`);
  for (const c of ['黑龙江', '酿酒概念']) ok(t.includes(c), `方向在：${c}`);
  ok(t.includes('86.1') && t.includes('76.5'), '带着评分');
  ok(t.includes('价没动·钱在进') && t.includes('首日净流入') && t.includes('位置偏低'),
     '依据（吸筹 / 资金刚动 / 位置偏低）都在');
  ok(t.includes('渠道库存'), '催化原文在');
  ok(t.includes('支线'), '支线定级在');
  ok(t.includes('口径') && t.includes('仓位'), '操作口径与仓位口径都给了');
  ok(t.includes('试错仓'), '退潮期写明只用试错仓');
  ok(t.includes('不是买入指令'), '免责说明在');
  ok(t.includes('-1.17%') && t.includes('+0.76'), '今日涨幅与主力净额');
  ok($('outlook').textContent.includes('明日'), '★ 顺带验证明日判断卡没被我改坏');

  console.log('\n[2] 闸门没过 → 不硬凑方向，且说清缺哪个');
  ({ $ } = await boot(mk(INACTIVE)));
  const t2 = $('rebound').textContent;
  ok(t2.includes('本轮不适用'), '标题写明本轮不适用');
  ok(t2.includes('存量博弈'), '缺的那个闸门点名');
  ok(!t2.includes('酿酒概念'), '不硬凑方向');
  ok(t2.includes('✅') && t2.includes('⛔'), '逐条标出闸门通过/未通过');

  console.log('\n[3] 模块关闭 / 报告里没这个键 → 不崩、不留空壳');
  ({ $ } = await boot(mk({ enabled: false, active: false, gate: {}, candidates: [], reason: '模块已关闭' })));
  ok(($('rebound').textContent || '').trim() === '', '关闭时卡片内容为空');
  ({ $ } = await boot(mk(null)));
  ok(($('rebound').textContent || '').trim() === '', '报告无 rebound 键时不渲染、不抛错');

  console.log('\n[4] 配置面板里有 E5，参数可编辑');
  ({ w, $ } = await boot(mk(ACTIVE)));
  w.fetch = (u) => {
    const s = String(u);
    if (s.startsWith('/api/config')) {
      return Promise.resolve({ status: 200, json: () => Promise.resolve({
        config: Object.assign({}, base.config, {
          E5_rebound: { enabled: true, max_pct: 1, min_main: 0.3, pos_days: 3,
                        top_n: 5, min_score: 55, trial_weight: 0.05 } }),
        presets: [] }) });
    }
    if (s.startsWith('/api/sources')) {
      return Promise.resolve({ status: 200, json: () => Promise.resolve(
        { ok: true, items: [], groups: [], summary: {}, blocked: [], push2: {}, tdx: {}, health: {} }) });
    }
    return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
  };
  await w.loadConfig();
  await sleep(250);
  const rows = [...w.document.querySelectorAll('#modules .mb')];
  const e5 = rows.find(d => d.getAttribute('data-m') === 'E5_rebound');
  ok(!!e5, '策略面板出现 E5_rebound 模块');
  if (e5) {
    const keys = [...e5.querySelectorAll('input[data-key]')].map(i => i.getAttribute('data-key'));
    ok(keys.length >= 5, `E5 的参数可编辑（${keys.join('、')}）`);
    ok(keys.includes('max_pct') && keys.includes('trial_weight'),
       '核心参数在（max_pct / trial_weight）');
  }

  console.log(`\n===== ${pass} 通过 / ${fail} 失败 =====`);
  process.exit(fail ? 1 : 0);
})();
