// 前端健壮性回归 —— 「字段缺失/降级时页面不能渲染 NaN / undefined」。
//
// 背景：2026-10-13 审计发现多处前端兜底口径不一致：
//   · 决策步骤①/③/⑤ 的进度条与文案直接 `*100`、`.toFixed(0)`（缺字段 → NaN%）
//   · 步骤② 的 pctv 在 gauge 为 null 时兜底成写死的 50%（拿假数据冒充读数）
//   · scoreLine 直接 `v.toFixed(0)`（分数缺失 → 抛异常，整张选股表连带 render 中断）
//   · renderDetail 的 `const g=p.graph_detail`（K线不足时后端给 {} → NaN%，手机版有兜底）
// 本测试钉死：**正常报告不回归** + **降级报告不出 NaN/undefined、不抛异常**。
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
const base = JSON.parse(fs.readFileSync(path.join(__dirname, '_test_report.json'), 'utf8'));

let pass = 0, fail = 0;
const ok = (c, m) => { c ? pass++ : fail++; console.log((c ? '  ✅ ' : '  ❌ ') + m); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

const CONFIG = {
  name: '野人体系', desc: '测试', preset: 'wildman',
  M2_volume: { enabled: true, peak_threshold: 15000, comfort_threshold: 12000,
               shrink_threshold: 8000, shrink_red_flag: true },
  M3_sentiment: { enabled: true, ebb_hurt_ratio: 0.3 },
  M4_aesthetic: { enabled: true, price_min: 5, price_max: 10, mv_max: 100,
                  mv_mid_threshold: 500, exclude_boards: [],
                  weight_graph: 35, weight_price_mv: 30, weight_imagination: 35,
                  recent_limitup_days: 20, recent_limitup_min: 1 },
  M5_position: { enabled: true, pick_count: 4, first_weight: 0.2, cash_reserve: 0.2 },
  M8_discipline: { enabled: true, rules: [] },
  M11_swing: { enabled: true, stall_pct: 3.0, converge_pct: 0.03 },
  E1_meter: { enabled: true, ebb_hurt_ratio: 0.3 },
  E2_lhb: { enabled: true, top_n: 12 },
  E3_flow: { enabled: true, board_top_n: 10 },
  E4_journal: { enabled: true, cost_pct: 0.002 },
};

function boot(report) {
  const dom = new JSDOM(html, { runScripts: 'dangerously', pretendToBeVisual: true,
                                url: 'http://127.0.0.1:8777/' });
  const w = dom.window;
  w.fetch = (url) => {
    const u = String(url);
    if (u.startsWith('/api/config')) {
      return Promise.resolve({ status: 200, json: () => Promise.resolve({ config: CONFIG, presets: [] }) });
    }
    if (u.startsWith('/api/result')) {
      return Promise.resolve({ status: 200, json: () => Promise.resolve(report) });
    }
    if (u.startsWith('/api/live')) {
      return Promise.resolve({ status: 200, json: () => Promise.resolve({ ts: Date.now() / 1000, quotes: {} }) });
    }
    return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
  };
  w.scrollTo = () => {};
  w.confirm = () => true;
  return { dom, w };
}

// 文本体检：页面正文里不该出现 NaN / undefined / Infinity（"—" 才是缺值的正确表达）
// ⚠️ 必须剔除 <script>/<style> —— textContent 会把内联脚本源码也算进来，
//    否则代码注释里写个 "NaN" 就会误报（第一版就踩了这个坑）。
function dirtyText(w) {
  const clone = w.document.body.cloneNode(true);
  clone.querySelectorAll('script,style').forEach(n => n.remove());
  const t = clone.textContent || '';
  const hits = [];
  for (const bad of ['NaN', 'undefined', 'Infinity']) {
    if (t.includes(bad)) hits.push(bad);
  }
  return hits;
}

(async () => {
  // ---------- [1] 正常报告：不得回归 ----------
  console.log('[1] 正常报告照常渲染');
  {
    const { w } = boot(base);
    w.finish(base);
    await sleep(500);
    const hits = dirtyText(w);
    ok(hits.length === 0, `正文无 NaN/undefined（命中：${hits.join(',') || '无'}）`);
    ok((w.document.querySelectorAll('#steps .step').length || 0) >= 4, '决策步骤已渲染');
    ok(w.document.querySelector('#pickBody') !== null, '选股表容器存在');
  }

  // ---------- [2] 降级报告：大量字段缺失也不能崩 ----------
  console.log('\n[2] 降级报告（字段大面积缺失）不抛异常、不出 NaN');
  {
    const deg = JSON.parse(JSON.stringify(base));
    delete deg.macro;
    delete deg.discipline;
    if (deg.sentiment) { delete deg.sentiment.metrics; delete deg.sentiment.max_boards; }
    if (deg.position) { delete deg.position.target_total; delete deg.position.chain_stage; }
    deg.volume = { mode: 'peak_swing', label: '峰值量能', amount: null, cum_amount: 10082,
                   intraday: true, pace: null, pace_txt: '', gauge: null, advice: '测试' };
    // 个股：分数缺一路、graph_detail 给空对象（= K 线不足时后端的返回）
    (deg.picks || []).forEach((p, i) => {
      if (i === 0) { p.scores = {}; p.graph_detail = {}; }
      if (i === 1) { delete p.score; }
    });
    const { w } = boot(deg);
    let threw = null;
    try { w.finish(deg); await sleep(500); } catch (e) { threw = e; }
    ok(!threw, `w.finish 未抛异常${threw ? '：' + threw.message : ''}`);
    const hits = dirtyText(w);
    ok(hits.length === 0, `正文无 NaN/undefined（命中：${hits.join(',') || '无'}）`);
  }

  // ---------- [3] 进度条：没有依据就画斜纹「无刻度」条 ----------
  console.log('\n[3] gauge / risk 缺失 → 斜纹无刻度条（不再拿写死的 50% / 55% 冒充读数）');
  {
    const deg = JSON.parse(JSON.stringify(base));
    deg.volume = Object.assign({}, deg.volume, { gauge: null });
    deg.macro = Object.assign({}, deg.macro, { risk: 'neutral' });   // 非 low/medium/high
    const { w } = boot(deg);
    w.finish(deg);
    await sleep(500);
    const unk = w.document.querySelectorAll('#steps .bar3 i.unk').length;
    ok(unk >= 2, `至少两根斜纹条（量能 + 宏观）：实际 ${unk} 根`);
  }

  // ---------- [4] scoreLine：缺分数显示「—」，不再抛异常 ----------
  console.log('\n[4] scoreLine 对缺失/非法分数要兜底');
  {
    const { w } = boot(base);
    w.finish(base);
    await sleep(400);
    for (const [v, want, label] of [[undefined, '—', 'undefined'],
                                    [null, '—', 'null'],
                                    [NaN, '—', 'NaN'],
                                    ['abc', '—', '非数字'],
                                    [88, '88', '正常值']]) {
      let got = '';
      try { got = String(w.scoreLine('图形', v)); } catch (e) { got = 'THREW:' + e.message; }
      ok(got.includes(want) && !/NaN/.test(got), `scoreLine(${label}) 含「${want}」`);
    }
  }

  // ---------- [5] 未知形态：REPORT 直接给空对象也不能白屏 ----------
  console.log('\n[5] 空报告不白屏（降级到"没有数据"而不是抛异常）');
  {
    const { w } = boot({});
    let threw = null;
    try { w.finish({}); await sleep(400); } catch (e) { threw = e; }
    ok(!threw, `w.finish({}) 未抛异常${threw ? '：' + threw.message : ''}`);
    const hits = dirtyText(w);
    ok(hits.length === 0, `正文无 NaN/undefined（命中：${hits.join(',') || '无'}）`);
  }

  console.log(`\n===== ${pass} 通过 / ${fail} 失败 =====`);
  process.exit(fail ? 1 : 0);
})();
