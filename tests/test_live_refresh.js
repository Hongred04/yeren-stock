// 桌面端静默刷新链路（index.html）—— 「没变化啊还是昨天的数据」（用户 10-13）
//
// 后端修的是 `/api/live` 在内存缓存过期后丢掉 result_ts（见 test_live_refresh.py）。
// 前端这边原来有两个缺口：
//   ① 端出来的是「上次结果」（服务器标了 stale / 报告不是今天的）时，
//      既不提示也不补扫 —— 页面就停在旧数据上；
//   ② 换新报告完全依赖 result_ts 变化，result_ts 一丢就永久冻结。
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
const base = JSON.parse(fs.readFileSync(path.join(__dirname, '_test_report.json'), 'utf8'));

let pass = 0, fail = 0;
const ok = (c, m) => { c ? pass++ : fail++; console.log((c ? '  ✅ ' : '  ❌ ') + m); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

const TODAY = (() => {
  const n = new Date();
  return '' + n.getFullYear() + String(n.getMonth() + 1).padStart(2, '0')
    + String(n.getDate()).padStart(2, '0');
})();

// 注意：必须保留真实 picks 的完整字段 —— 换成最小对象会让 render() 抛异常
// （数据里少个字段就会），那就测不到"换新报告"了，测的是"渲染崩了"。
function mkReport({ ts, date = TODAY, stale = false, firstName } = {}) {
  const r = JSON.parse(JSON.stringify(base));
  r.meta = Object.assign({}, r.meta, { ts, date });
  if (stale) { r.stale = true; r.stale_ts = ts; }
  if (firstName && r.picks && r.picks[0]) r.picks[0].name = firstName;
  return r;
}
const PICK_NAME = (base.picks && base.picks[0] && base.picks[0].name) || '';

(async () => {
  const T1 = 1000, T2 = 2000;
  let startCalls = 0, resultPayload = mkReport({ ts: T1, stale: true, firstName: '甲股' });
  let livePayload = { ts: Date.now() / 1000, phase: '早盘', quotes: {}, result_ts: null };

  const dom = new JSDOM(html, { runScripts: 'dangerously', pretendToBeVisual: true,
                                url: 'http://127.0.0.1:8777/' });
  const w = dom.window;
  w.fetch = (url) => {
    const u = String(url);
    if (u.startsWith('/api/has-cache')) {
      return Promise.resolve({ status: 200, json: () => Promise.resolve(
        { cached: true, running: false, pct: 100 }) });
    }
    if (u.startsWith('/api/start')) {
      startCalls++;
      return Promise.resolve({ status: 200, json: () => Promise.resolve({ ok: true }) });
    }
    if (u.startsWith('/api/result')) {
      return Promise.resolve({ status: 200, json: () => Promise.resolve(resultPayload) });
    }
    if (u.startsWith('/api/live')) {
      return Promise.resolve({ status: 200, json: () => Promise.resolve(livePayload) });
    }
    if (u.startsWith('/api/sources')) {
      return Promise.resolve({ status: 200, json: () => Promise.resolve(
        { ok: true, items: [], groups: [], summary: {}, blocked: [], push2: {}, tdx: {}, health: {} }) });
    }
    return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
  };
  w.scrollTo = () => {};

  const $ = id => w.document.getElementById(id);
  // 顶层 let 声明的变量（REPORT / _lastTs）不会挂到 window 上 —— 必须 eval 读
  const g = expr => w.eval(expr);

  console.log('\n[1] 端出「上次结果」时必须提示 + 自动补扫');
  await w.load();
  await sleep(600);
  ok(!!$('main') && !$('main').classList.contains('hide'), '主界面渲染出来了');
  ok(/上次结果/.test($('hdDate').textContent),
     `日期处标了「上次结果」（${$('hdDate').textContent}）`);
  ok(startCalls >= 1, `自动补扫了一次（/api/start 调用 ${startCalls} 次）`);

  console.log('\n[2] _reportIsOld：stale 与「不是今天」都要判为过期');
  ok(w._reportIsOld(mkReport({ ts: T1, stale: true })) === true, 'stale=true → 过期');
  ok(w._reportIsOld(mkReport({ ts: T1, date: '20260101' })) === true, '日期不是今天 → 过期');
  ok(w._reportIsOld(mkReport({ ts: T1, date: TODAY })) === false, '今天的新报告 → 不过期');
  ok(w._reportIsOld(null) === false, '空报告不报错');

  console.log('\n[3] 今天的新报告不提示「上次结果」');
  resultPayload = mkReport({ ts: T1, date: TODAY, stale: false, firstName: '乙股' });
  await w.load();
  await sleep(400);
  ok(!/上次结果/.test($('hdDate').textContent),
     `没有误标（${$('hdDate').textContent}）`);
  ok(new RegExp(TODAY).test($('hdDate').textContent), '显示今天的日期');

  console.log('\n[4] result_ts 变化 → 静默换上新报告（这条断了页面就永久冻结）');
  ok(Number(g('_lastTs')) === T1, `_lastTs 记录了当前报告的版本（${g('_lastTs')}）`);
  // 服务器端新扫完成：result_ts 变了，/api/result 也给新的一份
  livePayload = { ts: Date.now() / 1000, phase: '早盘', quotes: {}, result_ts: T2 };
  resultPayload = mkReport({ ts: T2, date: TODAY, stale: false, firstName: '丙股' });
  await w.silentRefresh();
  await sleep(300);
  ok(Number(g('_lastTs')) === T2, `_lastTs 已跟进到 ${T2}`);
  ok(Number(g('REPORT && REPORT.meta && REPORT.meta.ts')) === T2, 'REPORT 换成了新的一份');
  ok(new RegExp('丙股').test(w.document.body.textContent), '界面渲染出了新报告的候选股');
  ok(!/上次结果/.test($('hdDate').textContent), '换新后「上次结果」标记消失');

  console.log('\n[5] result_ts 丢失时不会误判（后端已保证它有值，这里兜底）');
  const before = Number(g('_lastTs'));
  livePayload = { ts: Date.now() / 1000, phase: '早盘', quotes: {}, result_ts: null };
  await w.silentRefresh();
  await sleep(200);
  ok(Number(g('_lastTs')) === before, 'result_ts 为空时不误换、也不清空版本号');

  console.log('\n[6] 渲染出错也不能拖垮自动刷新（finish 用 finally 兜底）');
  // 实测发现：render() 一旦抛异常（数据里少个字段就会），原来 startAuto() 永远
  // 执行不到 —— 静默刷新彻底不启动，页面从此冻结。这里人为让 render 抛一次。
  w.eval(`
    stopAuto();
    _lastTs = 0;
    window.__realRender = render;
    render = function(){ throw new Error('模拟渲染异常'); };
  `);
  let threw = false;
  try { w.finish(mkReport({ ts: 3000, date: TODAY })); } catch (e) { threw = true; }
  await sleep(150);
  ok(threw, 'render 抛异常时 finish 如实抛出（不吞错）');
  ok(!!g('_liveTimer'), '★ 自动刷新仍然启动了（页面不会永久冻结）');
  ok(Number(g('_lastTs')) === 3000, `_lastTs 也记上了（${g('_lastTs')}）`);
  w.eval('render = window.__realRender;');
  w.eval('stopAuto()');

  console.log(`\n===== ${pass} 通过 / ${fail} 失败 =====`);
  process.exit(fail ? 1 : 0);
})();
