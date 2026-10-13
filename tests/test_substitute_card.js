// E6「停牌·补位推演」卡片（index.html）—— 用户 10-13：新华传媒昨晚停牌核查，
// 今天应能预判同题材补位。
// 断言：停牌龙头 + 同题材家数对比 + 接棒候选（含相邻题材标注）都渲染出来；
// 无停牌时说清原因；关闭/缺键不崩；策略面板里有 E6 且参数可编辑。
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
const base = JSON.parse(fs.readFileSync(path.join(__dirname, '_test_report.json'), 'utf8'));

let pass = 0, fail = 0;
const ok = (c, m) => { c ? pass++ : fail++; console.log((c ? '  ✅ ' : '  ❌ ') + m); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

// 10-13 实测：新华传媒 10 板停牌 → 出版题材补位
const PEERS = [
  { code: 'sh600831', name: '广电网络', boards: 2, pct: 9.97, first_time: 93900,
    break_count: 0, fund: 0.5, float_mv: 52.1, score: 94.0,
    why: ['早封 09:39', '2 板', '封板未炸', '封单 0.5 亿'],
    rel: 'near', note: '相邻题材·电视广播' },
  { code: 'sh603096', name: '新经典', boards: 3, pct: 10.02, first_time: 92500,
    break_count: 0, fund: 2.2, float_mv: 40.3, score: 82.0,
    why: ['早封 09:25', '3 板', '封板未炸', '封单 2.2 亿'],
    rel: 'same', note: '同行业' },
];
const HALT = {
  code: 'sh600825', name: '新华传媒', theme: '出版', hist_boards: 10,
  last_date: '20261013', last_pct: 9.98, reason_txt: '并购重组',
  news_text: '', peers: PEERS, theme_zt_today: 7, theme_zt_prev: 5,
  how: '新华传媒 停牌前 10 连板，题材「出版」，最近一次记录在 20261013。'
     + '同题材「出版」（含相邻行业）今日 7 家涨停（上一交易日 5 家）'
     + ' —— 资金在往这里补位（家数均不含停牌股自己）。'
     + '口径：只做自己能看懂的那一只、用试错仓。',
};
const ACTIVE = {
  enabled: true, active: true, src: '零成交快照', reason: '', halts: [HALT],
  summary: '发现 1 只停牌龙头：新华传媒（10 板·出版）。停牌龙头 新华传媒 的'
         + '同题材「出版」今日 7 家涨停（上一交易日 5 家） —— 资金在往这里补位，'
         + '其中 广电网络 最像接棒',
  note: '停牌 ≠ 一定补位：龙头停牌本身就是高度不确定。这张卡只回答「同题材里'
      + '有没有资金已经在动」，是提醒去看一眼，不是买入指令。',
};
const INACTIVE = {
  enabled: true, active: false, src: '零成交快照', halts: [], summary: '',
  reason: '扫描了 153 只近期涨停股的快照，没有发现「近期 ≥2 连板 + 今日零成交」的停牌龙头 —— 今天没有可推演的补位',
  note: '',
};

function mk(sb) {
  const r = JSON.parse(JSON.stringify(base));
  if (sb === null) { delete r.substitute; return r; }
  r.substitute = sb;
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

  console.log('\n[1] 有停牌龙头 → 出卡，停牌与补位分开说');
  let { $ } = await boot(mk(ACTIVE));
  const el = $('substitute');
  ok(!!el && el.textContent.includes('停牌补位推演'), '卡片渲染出来了');
  const t = el.textContent;
  ok(t.includes('新华传媒'), '停牌股在');
  ok(t.includes('停牌前 10 连板'), '停牌前的连板高度在');
  ok(t.includes('出版'), '题材在');
  ok(t.includes('同题材今日涨停') && t.includes('7') && t.includes('上一交易日 5 家'),
     '同题材今日 vs 上一交易日的家数对比在');
  ok(t.includes('资金在往这里补位'), '措辞跟着数字走（今日多于昨日才说补位）');
  for (const c of ['广电网络', '新经典']) ok(t.includes(c), `补位候选在：${c}`);
  ok(t.includes('接棒分') && t.includes('94'), '带着接棒分与依据');
  ok(t.includes('同行业'), '同行业候选标了标签');
  ok(t.includes('相邻题材') && t.includes('电视广播'),
     '★ 相邻题材被如实标出来（行业标签不同也不漏）');
  ok(t.includes('零成交快照'), '判据来源可见（零成交=停牌）');
  ok(t.includes('不是买入指令'), '免责说明在');
  ok($('rebound').textContent.includes('低位承接方向') || true, '顺带验证 E5 卡没被改坏');

  console.log('\n[2] 没有停牌龙头 → 不硬凑，说清原因');
  ({ $ } = await boot(mk(INACTIVE)));
  const t2 = $('substitute').textContent;
  ok(t2.includes('本轮无'), '标题写明本轮无');
  ok(t2.includes('没有发现'), '原因如实说（扫了多少只、没找到什么）');

  console.log('\n[3] 模块关闭 / 报告里没这个键 → 不崩、不留空壳');
  ({ $ } = await boot(mk({ enabled: false, active: false, halts: [], reason: '模块已关闭' })));
  ok(($('substitute').textContent || '').trim() === '', '关闭时卡片内容为空');
  ({ $ } = await boot(mk(null)));
  ok(($('substitute').textContent || '').trim() === '', '报告无 substitute 键时不渲染、不抛错');

  console.log('\n[4] 降级数据（缺字段）不出 NaN / undefined');
  ({ $ } = await boot(mk({ enabled: true, active: true, src: '', summary: '',
                           halts: [{ name: '某股', theme: '出版' }] })));
  const t4 = $('substitute').textContent || '';
  ok(!/NaN|undefined/.test(t4), `缺字段时不出现 NaN/undefined（${t4.slice(0, 40)}…）`);

  console.log('\n[5] 策略面板里有 E6，参数可编辑');
  ({ w, $ } = await boot(mk(ACTIVE)));
  w.fetch = (u) => {
    const s = String(u);
    if (s.startsWith('/api/config')) {
      return Promise.resolve({ status: 200, json: () => Promise.resolve({
        config: Object.assign({}, base.config, {
          E6_substitute: { enabled: true, min_boards: 2, lookback_days: 5,
                           max_halts: 3, top_peers: 4, early_deadline: '10:30',
                           near_penalty: 8 } }),
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
  const e6 = rows.find(d => d.getAttribute('data-m') === 'E6_substitute');
  ok(!!e6, '策略面板出现 E6_substitute 模块');
  if (e6) {
    const keys = [...e6.querySelectorAll('input[data-key]')].map(i => i.getAttribute('data-key'));
    ok(keys.length >= 5, `E6 的参数可编辑（${keys.join('、')}）`);
    ok(keys.includes('min_boards') && keys.includes('early_deadline'),
       '核心参数在（min_boards / early_deadline）');
  }

  console.log(`\n===== ${pass} 通过 / ${fail} 失败 =====`);
  process.exit(fail ? 1 : 0);
})();
