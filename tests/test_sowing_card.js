// E8「题材播种 · 前置题材池」卡片（index.html）—— 用户 10-13「打板要提前找题材」。
// 断言：题材生命周期（新播种/加固/持平/减弱/熄火）、独苗首板、重复启动（含标签漂移）
// 都渲染出来；无数据说清原因；关闭/缺键不崩；降级数据不出 NaN/undefined；
// 策略面板有 E8 且参数可编辑。
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
const base = JSON.parse(fs.readFileSync(path.join(__dirname, '_test_report.json'), 'utf8'));

let pass = 0, fail = 0;
const ok = (c, m) => { c ? pass++ : fail++; console.log((c ? '  ✅ ' : '  ❌ ') + m); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

const ACTIVE = {
  enabled: true, active: true, reason: '', mode: '题材播种', date: '20261013',
  themes: [
    { theme: '元件', trend: 'add', label_cn: '加固中', today_n: 9, prev_n: 4,
      max_boards: 1, leaders: ['元件A', '元件B'], events: ['政策/规划'], why: '' },
    { theme: '分散染料', trend: 'new', label_cn: '新播种', today_n: 1, prev_n: 0,
      max_boards: 1, leaders: ['染料A'], events: ['涨价'], why: '' },
    { theme: '化学制品', trend: 'gone', label_cn: '熄火', today_n: 0, prev_n: 7,
      max_boards: 0, leaders: [], events: [], why: '' },
  ],
  lonely: [
    { code: 'sz002242', name: '九阳股份', theme: '小家电', label: '家电+机器人概念',
      pct: 9.98, first_time: 92500, break_count: 0, events: ['互动易'],
      new_theme: false, hist_days: 3, score: 85.0,
      why: '「小家电」今日只有 1 只以内的票涨停…' },
  ],
  restarts: [
    { code: 'sz002242', name: '九阳股份', label: '电子商务+家电',
      labels: ['家电+机器人概念', '电子商务+家电'],
      launches: [{ date: '20260911', boards: 1, label: '家电+机器人概念' },
                 { date: '20260928', boards: 1, label: '机器人概念+家电' }],
      launch_count: 2, gap_days: 9, max_boards: 6, events: ['互动易'],
      drifted: true, still: false, score: 148.0,
      why: '九阳股份 在近 30 个交易日里启动了 2 次…标签漂移…' },
  ],
  summary: '题材共 3 个可判读；新播种 1 个（分散染料）；重复启动 1 只，其中标签漂移 1 只（九阳股份）。',
  note: '这张卡是**备选池**，不是买入信号。',
};
const INACTIVE = {
  enabled: true, active: false, mode: '题材播种', date: '20261013',
  themes: [], lonely: [], restarts: [], summary: '',
  reason: '本地还没有涨停池归档（冷启动）—— 题材生命周期与「重复启动」都要靠历史归档',
  note: '',
};

function mk(sw) {
  const r = JSON.parse(JSON.stringify(base));
  if (sw === null) { delete r.sowing; return r; }
  r.sowing = sw;
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

  console.log('\n[1] 有题材播种 → 出卡，三个栏目都在');
  let { $ } = await boot(mk(ACTIVE));
  const el = $('sowing');
  ok(!!el && el.textContent.includes('题材播种'), '卡片渲染出来了');
  const t = el.textContent;
  ok(t.includes('明天该盯哪几条线'), '标题点明模块要回答的问题');
  ok(t.includes('题材生命周期'), '栏目① 题材生命周期');
  ok(t.includes('独苗首板'), '栏目② 独苗首板');
  ok(t.includes('重复启动'), '栏目③ 重复启动');
  ok(t.includes('新播种') && t.includes('加固中') && t.includes('熄火'),
     '★ 五档状态词在（新播种/加固中/熄火）');
  ok(t.includes('九阳股份') && t.includes('标签漂移'), '★ 重复启动 + 标签漂移在');
  ok(t.includes('备选池') || t.includes('不是买入信号'), '免责/口径说明在');

  console.log('\n[2] 没有数据 → 不硬凑，说清原因');
  ({ $ } = await boot(mk(INACTIVE)));
  const t2 = $('sowing').textContent;
  ok(t2.includes('本轮无'), '标题写明本轮无');
  ok(t2.includes('冷启动'), '原因如实说');

  console.log('\n[3] 模块关闭 / 报告里没这个键 → 不崩、不留空壳');
  ({ $ } = await boot(mk({ enabled: false, active: false, themes: [], lonely: [], restarts: [], reason: '模块已关闭' })));
  ok(($('sowing').textContent || '').trim() === '', '关闭时卡片内容为空');
  ({ $ } = await boot(mk(null)));
  ok(($('sowing').textContent || '').trim() === '', '报告无 sowing 键时不渲染、不抛错');

  console.log('\n[4] 降级数据（缺字段）不出 NaN / undefined');
  ({ $ } = await boot(mk({ enabled: true, active: true, summary: '',
                           themes: [{ theme: '某题材', label_cn: '加固中' }],
                           lonely: [{ name: '某票', theme: '某题材' }],
                           restarts: [{ name: '某票' }] })));
  const t4 = $('sowing').textContent || '';
  ok(!/NaN|undefined/.test(t4), `缺字段不出现 NaN/undefined（${t4.slice(0, 42).replace(/\s+/g, ' ')}…）`);

  console.log('\n[5] 策略面板里有 E8，参数可编辑');
  let { w, $: $2 } = await boot(mk(ACTIVE));
  w.fetch = (u) => {
    const s = String(u);
    if (s.startsWith('/api/config')) {
      return Promise.resolve({ status: 200, json: () => Promise.resolve({
        config: Object.assign({}, base.config, {
          E8_sowing: { enabled: true, lookback_days: 30, top_themes: 8,
                       top_lonely: 3, top_restarts: 6, lonely_max_peers: 1,
                       drifted_only: false } }),
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
  const e8 = rows.find(d => d.getAttribute('data-m') === 'E8_sowing');
  ok(!!e8, '策略面板出现 E8_sowing 模块');
  if (e8) {
    const keys = [...e8.querySelectorAll('input[data-key]')].map(i => i.getAttribute('data-key'));
    ok(keys.length >= 5, `E8 的参数可编辑（${keys.join('、')}）`);
    ok(keys.includes('lookback_days') && keys.includes('lonely_max_peers'),
       '核心参数在（lookback_days / lonely_max_peers）');
  }

  console.log(`\n===== ${pass} 通过 / ${fail} 失败 =====`);
  process.exit(fail ? 1 : 0);
})();
