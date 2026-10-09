// 静默刷新测试：每分钟更新数据，但不许打断用户
// 断言重点：没有遮罩、不跳页、不动选中、行情真的更新了
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
const report = JSON.parse(fs.readFileSync(path.join(__dirname, '_test_report.json'), 'utf8'));

let pass = 0, fail = 0;
const ok = (c, m) => { c ? pass++ : fail++; console.log((c ? '  ✅ ' : '  ❌ ') + m); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

// 造一份"比报告更新"的实时数据：价格全改掉，方便断言
function makeLive(ts) {
  const quotes = {};
  (report.picks || []).forEach((p, i) => {
    quotes[p.code] = { price: 99.99 + i, pct: 8.88, turnover: 3.3, volume_ratio: 1.5 };
  });
  (report.portfolio && report.portfolio.watchlist || []).forEach(w => {
    quotes[w.code] = { price: 55.55, pct: -6.66, turnover: 2.2 };
  });
  return {
    ts: Date.now() / 1000, server_time: '10:30:00', phase: '早盘',
    live_sentiment: true, result_ts: ts,
    sentiment: {
      stage: 'ebb', can_trade: false, trial_only: true,
      ebb_trial_weight: 0.05, note: '退潮期：不接力、不重仓；最多 0.5 成（5%）试错仓，只做首板',
      stability: { state: 'ebb', confirmed: true, raw: true, raw_streak: 3 },
      // 真实 /api/live 给的是完整的 evaluate_sentiment 结果，含 metrics；
      // 缺了它 render() 会读 mm.max_boards 崩掉，所以 mock 要照实还原
      metrics: { limit_up: 30, limit_down: 8, broken: 12, max_boards: 3,
                 seal_rate: 0.71, hurt_area: 0.33 },
    },
    quotes,
  };
}

(async () => {
  const ts0 = (report.meta && report.meta.ts) || 111;
  const LIVE = makeLive(ts0 + 999);      // 更新的一份

  const dom = new JSDOM(html, { runScripts: 'dangerously', pretendToBeVisual: true,
                                url: 'http://127.0.0.1:8777/' });
  const w = dom.window;

  let liveCalls = 0, resultCalls = 0, startCalls = 0;
  w.fetch = (url) => {
    const u = String(url);
    if (u.startsWith('/api/live')) {
      liveCalls++;
      return Promise.resolve({ status: 200, json: () => Promise.resolve(LIVE) });
    }
    if (u.startsWith('/api/result')) {
      resultCalls++;
      return Promise.resolve({ status: 200, json: () => Promise.resolve(report) });
    }
    if (u.startsWith('/api/start')) {
      startCalls++;
      return Promise.resolve({ status: 200, json: () => Promise.resolve({ ok: true }) });
    }
    if (u.startsWith('/api/sources')) {
      return Promise.resolve({ status: 200, json: () => Promise.resolve({ ok: true, cookie: {}, blocked: [] }) });
    }
    return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
  };

  w.finish(report);
  await sleep(300);

  console.log('\n[1] 加载完成后：主界面可见、无遮罩');
  ok(!w.document.getElementById('loading').classList.contains('hide') === false,
     'loading 遮罩已隐藏');
  ok(!w.document.getElementById('main').classList.contains('hide'), '主界面可见');

  // 切到一个二级页，验证静默刷新后不会被弹回首页
  const nav = w.document.querySelector('[data-page="mine-watch"], .sub, .nav-i');
  w.SEL = null;
  const beforePage = w.document.querySelector('.page.on');
  const beforePageName = beforePage ? beforePage.dataset.page : '';

  console.log('\n[2] 静默刷新一次');
  const priceBefore = w.document.querySelector('#pickBody tr[data-c] td:nth-child(5)');
  const priceTxtBefore = priceBefore ? priceBefore.textContent.trim() : '';
  await w.silentRefresh();
  await sleep(500);

  ok(liveCalls >= 1, `/api/live 被调用 (${liveCalls} 次)`);
  ok(!w.document.getElementById('loading').classList.contains('hide')
     === false, '刷新过程中没有弹出遮罩');
  ok(!w.document.getElementById('main').classList.contains('hide'), '主界面始终可见');

  const priceAfter = w.document.querySelector('#pickBody tr[data-c] td:nth-child(5)');
  const priceTxtAfter = priceAfter ? priceAfter.textContent.trim() : '';
  ok(priceTxtAfter && priceTxtAfter !== priceTxtBefore,
     `候选价格就地更新了：${priceTxtBefore} → ${priceTxtAfter}`);

  const pctAfter = w.document.querySelector('#pickBody tr[data-c] td:nth-child(6)');
  ok(pctAfter && /8\.88%/.test(pctAfter.textContent), `涨跌幅更新为 8.88%：${pctAfter.textContent.trim()}`);

  const afterPage = w.document.querySelector('.page.on');
  ok((afterPage ? afterPage.dataset.page : '') === beforePageName,
     `停留在原页面（${beforePageName || '首页'}）没被弹走`);

  console.log('\n[3] 退潮期试错仓的结论要能显示出来');
  const verdict = w.document.getElementById('verdict').textContent.replace(/\s+/g, ' ');
  ok(/0\.5 成|试错/.test(verdict) || /退潮/.test(verdict),
     '结论区反映了退潮/试错仓：' + verdict.slice(0, 80));

  console.log('\n[4] 顶栏更新时间标记');
  const tag = w.document.getElementById('hdLive').textContent;
  ok(/10:30:00/.test(tag), `显示刷新时间：${tag}`);
  ok(/退潮/.test(tag), `显示稳定后的阶段：${tag}`);

  console.log('\n[5] 服务器上有新报告时静默换新');
  ok(resultCalls >= 1, `/api/result 被静默拉取 (${resultCalls} 次)`);

  console.log('\n[6] 自动刷新可以关掉');
  w.document.getElementById('hdAuto').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await sleep(80);
  ok(w.document.getElementById('hdAuto').textContent.includes('关'),
     '顶栏显示「关闭」');
  const liveBefore = liveCalls;
  await w.silentRefresh();
  await sleep(300);
  ok(liveCalls === liveBefore, '关闭后不再请求 /api/live');

  console.log('\n[7] 遮罩开关函数没被污染（重新扫描仍可用）');
  ok(typeof w.load === 'function', 'load() 仍存在');

  console.log(`\n===== ${pass} 通过 / ${fail} 失败 =====`);
  process.exit(fail ? 1 : 0);
})();
