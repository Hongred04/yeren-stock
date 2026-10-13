// E7「逆势连板 · 独立通道」卡片（index.html）—— 用户 10-13 的九阳股份案例。
// 断言：退潮日照样连板的票渲染出来，证据（缩量一字/炸板回封）、跨过的逆势日、
// 首板市值、以及"今天是否还在涨停池"都在；无票时说明原因；关闭/缺键不崩；
// 降级数据不出 NaN/undefined；策略面板里有 E7 且参数可编辑。
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
const base = JSON.parse(fs.readFileSync(path.join(__dirname, '_test_report.json'), 'utf8'));

let pass = 0, fail = 0;
const ok = (c, m) => { c ? pass++ : fail++; console.log((c ? '  ✅ ' : '  ❌ ') + m); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

const PICKS = [
  { code: 'sz002242', name: '九阳股份', sector: '电子商务', max_boards: 6,
    first_date: '20260928', last_date: '20261012',
    ebb_days: ['20261008', '20261012'], mv_first: 78.7, mv_ok: true, still: false,
    score: 110.0,
    flags: ['缩量一字（20261008，4板，换手 1.02% ← 前板 4.59%）',
            '炸板回封（20261009，5板，炸 6 次仍封住）'],
    how: '九阳股份 的连板链跨过了 2 个逆势日（20261008、20261012），最高 6 板，'
       + '首板流通市值约 79 亿。证据：缩量一字…。它今天已不在涨停池（断板或停牌）'
       + '—— 下面这些只当复盘看。' },
  { code: 'sh600825', name: '新华传媒', sector: '出版', max_boards: 10,
    first_date: '20260921', last_date: '20261012',
    ebb_days: ['20261008', '20261012'], mv_first: null, mv_ok: true, still: false,
    score: 90.0, flags: ['缩量一字（20260930，7板，换手 1.34% ← 前板 3.09%）'],
    how: '新华传媒 的连板链跨过了 2 个逆势日，最高 10 板。' },
];
const ACTIVE = {
  enabled: true, active: true, reason: '', mode: '逆势连板', picks: PICKS,
  summary: '逆势连板 2 只：九阳股份（6 板）、新华传媒（10 板）。'
         + '今天都已不在涨停池里（断板/停牌）。这批票的驱动不是情绪周期，'
         + '情绪闸门对它们不适用 —— 单独看。',
  note: '这张卡只回答「谁的连板不是情绪撑的」—— **不代表现在能买**。',
};
const INACTIVE = {
  enabled: true, active: false, mode: '逆势连板', picks: [], summary: '',
  reason: '扫了近 10 个归档交易日，没有连板 ≥3 板且跨过逆势日的票 —— 这一轮没有逆势连板',
  note: '',
};

function mk(ind) {
  const r = JSON.parse(JSON.stringify(base));
  if (ind === null) { delete r.industry; return r; }
  r.industry = ind;
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

  console.log('\n[1] 有逆势连板 → 出卡，证据与现状都在');
  let { $ } = await boot(mk(ACTIVE));
  const el = $('industry');
  ok(!!el && el.textContent.includes('逆势连板'), '卡片渲染出来了');
  const t = el.textContent;
  ok(t.includes('谁的连板不是情绪撑的'), '标题点明模块要回答的问题');
  for (const c of ['九阳股份', '新华传媒']) ok(t.includes(c), `票在：${c}`);
  ok(t.includes('最高 6 板') && t.includes('最高 10 板'), '连板高度在');
  ok(t.includes('缩量一字') && t.includes('1.02%'), '★ 缩量一字的证据在（含换手对比）');
  ok(t.includes('炸板回封') && t.includes('炸 6 次'), '★ 炸板回封的证据在');
  ok(t.includes('跨过逆势日 2 个') && t.includes('20261008'), '跨过的逆势日如实列出');
  ok(t.includes('首板日流通市值约 78.7 亿') || t.includes('78.7'), '首板市值在（日K首板价反推）');
  ok(t.includes('首板市值未取得'), '取不到市值的票如实标注，不编数字');
  ok(t.includes('今天已断板/停牌'), '★ 今天是否还在涨停池如实给出');
  ok(t.includes('不是买点') || t.includes('不代表现在能买'), '免责/口径说明在');

  console.log('\n[2] 没有逆势连板 → 不硬凑，说清原因');
  ({ $ } = await boot(mk(INACTIVE)));
  const t2 = $('industry').textContent;
  ok(t2.includes('本轮无'), '标题写明本轮无');
  ok(t2.includes('没有连板') || t2.includes('没有'), '原因如实说');

  console.log('\n[3] 模块关闭 / 报告里没这个键 → 不崩、不留空壳');
  ({ $ } = await boot(mk({ enabled: false, active: false, picks: [], reason: '模块已关闭' })));
  ok(($('industry').textContent || '').trim() === '', '关闭时卡片内容为空');
  ({ $ } = await boot(mk(null)));
  ok(($('industry').textContent || '').trim() === '', '报告无 industry 键时不渲染、不抛错');

  console.log('\n[4] 降级数据（缺字段）不出 NaN / undefined');
  ({ $ } = await boot(mk({ enabled: true, active: true, summary: '',
                           picks: [{ name: '某票' }] })));
  const t4 = $('industry').textContent || '';
  ok(!/NaN|undefined/.test(t4), `缺字段不出现 NaN/undefined（${t4.slice(0, 42).replace(/\s+/g, ' ')}…）`);

  console.log('\n[5] 策略面板里有 E7，参数可编辑');
  ({ w, $ } = await boot(mk(ACTIVE)));
  w.fetch = (u) => {
    const s = String(u);
    if (s.startsWith('/api/config')) {
      return Promise.resolve({ status: 200, json: () => Promise.resolve({
        config: Object.assign({}, base.config, {
          E7_industry: { enabled: true, min_boards: 3, lookback_days: 10, top_n: 5,
                         mv_max: 100, shrink_ratio: 0.7, reseal_breaks: 3,
                         mv_check: true } }),
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
  const e7 = rows.find(d => d.getAttribute('data-m') === 'E7_industry');
  ok(!!e7, '策略面板出现 E7_industry 模块');
  if (e7) {
    const keys = [...e7.querySelectorAll('input[data-key]')].map(i => i.getAttribute('data-key'));
    ok(keys.length >= 5, `E7 的参数可编辑（${keys.join('、')}）`);
    ok(keys.includes('min_boards') && keys.includes('mv_max'),
       '核心参数在（min_boards / mv_max）');
  }

  console.log(`\n===== ${pass} 通过 / ${fail} 失败 =====`);
  process.exit(fail ? 1 : 0);
})();
