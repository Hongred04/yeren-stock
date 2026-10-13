// 7 处已知缺陷的**前端侧**回归（后端侧见 tests/test_known_defects.py）
//   [1] M11 滞涨阈值渲染器：不能再走 pctf（保存会 ÷100，把 WS1 离场点弄失效）
//   [2] E2 净买入榜条数：有默认值，输入框不能再显示 undefined
//   [3] 关闭 M5 仓位系统：界面不能出现 NaN%
//   [4] 板块轮动：有数据才渲染、没数据整块隐藏
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
const report = JSON.parse(fs.readFileSync(path.join(__dirname, '_test_report.json'), 'utf8'));

let pass = 0, fail = 0;
const ok = (c, m) => { c ? pass++ : fail++; console.log((c ? '  ✅ ' : '  ❌ ') + m); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

// 一份"正常"的配置：M11 的 stall_pct 是百分数 3.0、E2 有 top_n
const CONFIG = {
  name: '野人体系', desc: '测试', preset: 'wildman',
  M2_volume: { enabled: true, peak_threshold: 15000, comfort_threshold: 12000,
               shrink_threshold: 8000, shrink_red_flag: true },
  M3_sentiment: { enabled: true, ebb_hurt_ratio: 0.3 },
  M4_aesthetic: { enabled: true, price_min: 5, price_max: 10, mv_max: 100,
                  mv_mid_threshold: 500, exclude_boards: [],
                  weight_graph: 35, weight_price_mv: 30, weight_imagination: 35 },
  M5_position: { enabled: true, pick_count: 4, first_weight: 0.2, cash_reserve: 0.2 },
  M8_discipline: { enabled: true, rules: [] },
  M11_swing: { enabled: true, stall_pct: 3.0, converge_pct: 0.03 },
  E1_meter: { enabled: true, ebb_hurt_ratio: 0.3 },
  E2_lhb: { enabled: true, top_n: 12 },
  E3_flow: { enabled: true, board_top_n: 10 },
  E4_journal: { enabled: true, cost_pct: 0.002 },
};

(async () => {
  const dom = new JSDOM(html, { runScripts: 'dangerously', pretendToBeVisual: true,
                                url: 'http://127.0.0.1:8777/' });
  const w = dom.window;
  w.fetch = (url) => {
    const u = String(url);
    if (u.startsWith('/api/config')) {
      return Promise.resolve({ status: 200, json: () => Promise.resolve(
        { config: CONFIG, presets: [] }) });
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

  console.log('\n[1] M11 滞涨阈值：渲染器不能是 pctf（data-pct 会把 3 存成 0.03）');
  await w.loadConfig();
  await sleep(200);
  const stall = w.document.querySelector('#modules input[data-key="stall_pct"]');
  ok(!!stall, '配置面板里有「滞涨判定·涨幅上限」输入框');
  ok(stall && !stall.hasAttribute('data-pct'),
     '输入框不带 data-pct（保存时不会被 ÷100）');
  ok(stall && Math.abs(parseFloat(stall.value) - 3) < 1e-9,
     `显示值就是百分数 3（实际 ${stall && stall.value}）`);

  console.log('\n[2] E2 净买入榜条数：有默认值，不显示 undefined');
  const topn = w.document.querySelector('#modules input[data-key="top_n"]');
  ok(!!topn, '配置面板里有「净买入榜展示条数」输入框');
  ok(topn && topn.value !== 'undefined' && topn.value !== '',
     `有实际数值（实际 ${topn && topn.value}）`);
  ok(topn && parseFloat(topn.value) === 12, '默认 12');

  console.log('\n[3] 关闭 M5：界面不能出现 NaN%');
  w.eval(`REPORT.position = ${JSON.stringify({
    chain: [], total: 0, target_total: 0.0, chain_stage: 'disabled',
    note: '仓位模块已关闭，自行控制',
  })}`);
  w.render();
  await sleep(100);
  const steps = $('steps') ? $('steps').textContent : '';
  const posWrap = $('posWrap') ? $('posWrap').textContent : '';
  const verdict = $('verdict') ? $('verdict').textContent : '';
  const all = steps + posWrap + verdict;
  ok(all.length > 0, '渲染出来了');
  ok(!/NaN/.test(all), '决策步骤 / 仓位卡 / 结论卡 都没有 NaN');
  ok(!/undefined/.test(all), '也没有 undefined');
  ok(/0%/.test(steps), `总仓位显示 0%（不是 NaN%）`);

  console.log('\n[4] 板块轮动：有数据渲染、没数据隐藏');
  const rot = [
    { date: '2026-10-08', plates: [{ name: '航运', pct: 2.2, rank: 1 },
                                   { name: '天然气', pct: 1.8, rank: 2 }] },
    { date: '2026-10-09', plates: [{ name: '短剧', pct: 6.5, rank: 1 },
                                   { name: '航运', pct: 2.2, rank: 2 }] },
  ];
  w.eval(`REPORT.sectors = ${JSON.stringify({
    items: [{ name: '半导体', role: '主线', level: '强', facts: ['5 家涨停'], judge: '看龙头' }],
    strongest: { name: '半导体', facts: ['5 家涨停'] },
    summary: '测试', data_note: '', rotation: rot,
  })}`);
  w.renderSectors();
  await sleep(60);
  const sec = $('secView').textContent;
  ok(/板块轮动/.test(sec), '有数据时渲染出「板块轮动」');
  ok(/短剧/.test(sec) && /航运/.test(sec), '列出各日领涨板块');
  ok(/近 2 日板块轮动/.test(sec), '标题带天数');
  ok(/新进/.test(sec) && /短剧/.test(sec), '标出最新一日新进的方向');

  // 抓不到数据 → 整块隐藏（不编造方向）
  w.eval(`REPORT.sectors = ${JSON.stringify({
    items: [{ name: '半导体', role: '主线', level: '强', facts: [], judge: '' }],
    strongest: { name: '半导体', facts: [] }, summary: '', data_note: '', rotation: [],
  })}`);
  w.renderSectors();
  await sleep(60);
  ok(!/板块轮动/.test($('secView').textContent), 'rotation 为空时整块隐藏');

  // 连 items 都没有（无板块合力）时，也要能正常渲染且不报错
  w.eval(`REPORT.sectors = ${JSON.stringify({
    items: [], strongest: {}, summary: '', data_note: '暂无板块合力数据', rotation: rot,
  })}`);
  let threw = false;
  try { w.renderSectors(); } catch (e) { threw = true; }
  await sleep(60);
  ok(!threw, '没有板块合力时 renderSectors 不抛异常');
  ok(/板块轮动/.test($('secView').textContent), '此时轮动数据仍然展示（两块互不依赖）');

  console.log(`\n===== ${pass} 通过 / ${fail} 失败 =====`);
  process.exit(fail ? 1 : 0);
})();
