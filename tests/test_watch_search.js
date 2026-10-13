// 加自选：名称搜索（桌面端 index.html）
// 需求：自选股原来只能输代码，记不住代码就加不进去；改为输**名称关键词**即可
// 匹配并添加，同时**保留按代码添加**。断言输入识别、结果下拉、点击加入，
// 以及「空输入 / 无匹配 / 接口异常」三种情况的处理。
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
const report = JSON.parse(fs.readFileSync(path.join(__dirname, '_test_report.json'), 'utf8'));

let pass = 0, fail = 0;
const ok = (c, m) => { c ? pass++ : fail++; console.log((c ? '  ✅ ' : '  ❌ ') + m); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

// 假搜索接口：关键词 → 结果（模拟后端 /api/search 的输出形状）
const SEARCH = {
  '银行': {
    ok: true, source: 'local', note: '',
    results: [
      { code: 'sh600036', name: '招商银行', board: '沪市主板', pinyin: '' },
      { code: 'sh600000', name: '浦发银行', board: '沪市主板', pinyin: '' },
    ],
  },
  '茅台': {
    ok: true, source: 'live+local', note: '',
    results: [{ code: 'sh600519', name: '贵州茅台', board: '沪市主板', pinyin: 'gzmt' }],
  },
  '找不到': { ok: true, source: 'live+local', note: '', results: [] },
  '断网': {
    ok: false, source: 'error',
    note: '搜索接口这次没响应（OSError），本地名单也没匹配到；也可以直接输 6 位代码（如 600519）',
    results: [],
  },
};

(async () => {
  const dom = new JSDOM(html, { runScripts: 'dangerously', pretendToBeVisual: true,
                                url: 'http://127.0.0.1:8777/' });
  const w = dom.window;

  let searchCalls = [];
  let portfolioPosts = [];
  const WATCH = [{ code: 'sh600036', name: '招商银行', price: 10, pct: 1, turnover: 2 }];
  const portView = () => ({
    ok: true,
    portfolio: {
      raw: { holdings: [], watchlist: WATCH.map(x => ({ code: x.code, name: x.name })) },
      holdings: [], watchlist: WATCH, total_cost: 0, total_mv: 0, total_pnl: 0,
      n_holdings: 0, n_watch: WATCH.length, checks: [], note: '',
    },
  });

  w.fetch = (url, opt) => {
    const u = String(url);
    if (u.startsWith('/api/search')) {
      const q = decodeURIComponent((u.match(/q=([^&]*)/) || [, ''])[1]);
      searchCalls.push(q);
      const body = SEARCH[q] || { ok: true, source: 'live+local', note: '', results: [] };
      return Promise.resolve({ status: 200, json: () => Promise.resolve(body) });
    }
    if (u.startsWith('/api/portfolio')) {
      if (opt && opt.method === 'POST') {
        const payload = JSON.parse(opt.body);
        portfolioPosts.push(payload);
        if (payload.action === 'watch_add' && payload.code
            && !WATCH.some(x => x.code === payload.code)) {
          WATCH.push({ code: payload.code, name: payload.name || payload.code,
                       price: 0, pct: 0, turnover: 0 });
        }
      }
      return Promise.resolve({ status: 200, json: () => Promise.resolve(portView()) });
    }
    if (u.startsWith('/api/result')) {
      return Promise.resolve({ status: 200, json: () => Promise.resolve(report) });
    }
    if (u.startsWith('/api/live')) {
      return Promise.resolve({ status: 200, json: () => Promise.resolve(
        { ts: Date.now() / 1000, quotes: {}, sentiment: null }) });
    }
    return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
  };
  w.scrollTo = () => {};
  w.confirm = () => true;

  w.finish(report);
  await sleep(400);

  const $ = id => w.document.getElementById(id);
  const setVal = (v) => {
    $('wlCode').value = v;
    $('wlCode').dispatchEvent(new w.Event('input', { bubbles: true }));
  };
  const clickAdd = () => { $('wlAdd').dispatchEvent(new w.Event('click', { bubbles: true })); };

  console.log('\n[1] 打开「管理」面板：自选输入框存在且提示说明了两种方式');
  await w.openPort();
  await sleep(150);
  ok(!!$('wlCode') && !!$('wlAdd') && !!$('wlSug'), '自选输入框 / 加自选按钮 / 结果下拉都在');
  ok(/名称/.test($('wlHint').textContent) && /代码/.test($('wlHint').textContent),
     '提示同时说明「名称」和「代码」两种输入');

  console.log('\n[2] 输名称关键词 → 逐字搜索出下拉候选（防抖后触发）');
  setVal('银行');
  await sleep(400);
  const sug = $('wlSug');
  ok(searchCalls.includes('银行'), `/api/search 被调用（${JSON.stringify(searchCalls)}）`);
  ok(sug.classList.contains('on'), '下拉已展开');
  ok(/招商银行/.test(sug.textContent) && /浦发银行/.test(sug.textContent), '列出匹配的银行股');
  ok(/sh600036/.test(sug.textContent) && /沪市主板/.test(sug.textContent), '每行显示代码与板块');
  ok(sug.querySelectorAll('.row').length === 2, '两行结果');

  console.log('\n[3] 已在自选的要标出来，且点了不会重复添加');
  const firstRow = sug.querySelector('.row[data-c="sh600036"]');
  ok(firstRow.classList.contains('dis'), '招商银行（已在自选）标为不可重复添加');
  ok(/已在自选/.test(firstRow.textContent), '行内显示「已在自选」');
  portfolioPosts.length = 0;
  firstRow.dispatchEvent(new w.Event('click', { bubbles: true }));
  await sleep(200);
  ok(portfolioPosts.length === 0, '点「已在自选」的行不会发请求');

  console.log('\n[4] 点未添加的结果 → 立即加入自选（名称一并带上）');
  const secondRow = sug.querySelector('.row[data-c="sh600000"]');
  ok(secondRow && !secondRow.classList.contains('dis'), '浦发银行可选');
  secondRow.dispatchEvent(new w.Event('click', { bubbles: true }));
  await sleep(250);
  const add = portfolioPosts.find(p => p.action === 'watch_add');
  ok(!!add, '发出了 watch_add 请求');
  ok(add && add.code === 'sh600000', `代码正确（${add && add.code}）`);
  ok(add && add.name === '浦发银行', `名称一并提交（${add && add.name}）`);
  ok($('wlCode').value === '', '加入后输入框已清空');

  console.log('\n[5] 名称唯一命中 → 点「加自选」直接加入，不用再选');
  setVal('茅台');
  await sleep(350);
  portfolioPosts.length = 0;
  clickAdd();
  await sleep(300);
  const add2 = portfolioPosts.find(p => p.action === 'watch_add');
  ok(!!add2 && add2.code === 'sh600519', `唯一命中直接加（${add2 && add2.code}）`);

  console.log('\n[6] 代码方式保留：输代码不发搜索请求，直接加入');
  searchCalls.length = 0;
  portfolioPosts.length = 0;
  setVal('600519');
  await sleep(300);
  ok(searchCalls.length === 0, '识别为代码 → 不发搜索请求');
  ok(/识别为代码/.test($('wlHint').textContent) && /sh600519/.test($('wlHint').textContent),
     '提示已识别成代码 sh600519');
  clickAdd();
  await sleep(300);
  const add3 = portfolioPosts.find(p => p.action === 'watch_add');
  ok(!!add3 && add3.code === 'sh600519', `按代码加入（${add3 && add3.code}）`);
  // 券商格式同样识别
  portfolioPosts.length = 0;
  setVal('000001.SZ');
  await sleep(300);
  clickAdd();
  await sleep(300);
  const add4 = portfolioPosts.find(p => p.action === 'watch_add');
  ok(!!add4 && add4.code === 'sz000001', `000001.SZ 这类券商格式也能识别（${add4 && add4.code}）`);

  console.log('\n[7] 输入为空 → 不发请求，提示要输什么');
  searchCalls.length = 0;
  portfolioPosts.length = 0;
  setVal('');
  await sleep(300);
  ok(searchCalls.length === 0, '空输入不发搜索请求');
  ok(!$('wlSug').classList.contains('on'), '空输入时下拉收起');
  clickAdd();
  await sleep(200);
  ok(/请输入/.test($('wlHint').textContent), `点「加自选」提示输入（${$('wlHint').textContent}）`);
  ok(portfolioPosts.length === 0, '空输入不会误加任何股票');

  console.log('\n[8] 无法匹配 → 下拉里给出明确说明，不静默失败');
  setVal('找不到');
  await sleep(400);
  ok($('wlSug').classList.contains('on'), '无匹配时下拉仍然展开（给出说明）');
  ok(/未匹配到/.test($('wlSug').textContent), `文案：${$('wlSug').textContent.trim()}`);

  console.log('\n[9] 搜索接口异常 → 如实提示并引导改用代码');
  setVal('断网');
  await sleep(400);
  ok(/没响应|没匹配/.test($('wlSug').textContent), `下拉说明：${$('wlSug').textContent.trim()}`);
  portfolioPosts.length = 0;
  clickAdd();
  await sleep(300);
  ok(portfolioPosts.length === 0, '异常时不会误加');

  // ── 手机端：底部弹出表单「加自选」同样支持名称搜索 ──
  console.log('\n[10] 手机端 mobile.html：加自选表单支持名称搜索');
  const mhtml = fs.readFileSync(path.join(__dirname, '..', 'web', 'mobile.html'), 'utf8');
  const mdom = new JSDOM(mhtml, { runScripts: 'dangerously', pretendToBeVisual: true,
                                  url: 'http://127.0.0.1:8777/mobile.html' });
  const mw = mdom.window;
  let mSearch = [], mPosts = [];
  mw.fetch = (url, opt) => {
    const u = String(url);
    if (u.startsWith('/api/search')) {
      const q = decodeURIComponent((u.match(/q=([^&]*)/) || [, ''])[1]);
      mSearch.push(q);
      const body = SEARCH[q] || { ok: true, source: 'live+local', note: '', results: [] };
      return Promise.resolve({ status: 200, json: () => Promise.resolve(body) });
    }
    if (u.startsWith('/api/portfolio')) {
      if (opt && opt.method === 'POST') mPosts.push(JSON.parse(opt.body));
      return Promise.resolve({ status: 200, json: () => Promise.resolve(portView()) });
    }
    if (u.startsWith('/api/result')) {
      return Promise.resolve({ status: 200, json: () => Promise.resolve(report) });
    }
    if (u.startsWith('/api/status')) {
      return Promise.resolve({ status: 200, json: () => Promise.resolve(
        { done: true, pct: 100, msg: '完成' }) });
    }
    return Promise.resolve({ status: 204, json: () => Promise.resolve({}) });
  };
  mw.scrollTo = () => {};
  await sleep(1500);

  mw.openWatchForm();
  await sleep(150);
  const m$ = id => mw.document.getElementById(id);
  ok(!!m$('fQuery') && !!m$('fSug'), '表单里是「名称 或 代码」输入框 + 候选列表');
  ok(!m$('fCode'), '旧的「只填代码」输入框已移除');

  m$('fQuery').value = '银行';
  m$('fQuery').dispatchEvent(new mw.Event('input', { bubbles: true }));
  await sleep(400);
  ok(mSearch.includes('银行'), `/api/search 被调用（${JSON.stringify(mSearch)}）`);
  ok(/招商银行/.test(m$('fSug').textContent), '手机端也列出候选');
  const mRow = m$('fSug').querySelector('.row[data-c="sh600000"]');
  mRow.dispatchEvent(new mw.Event('click', { bubbles: true }));
  await sleep(300);
  const mAdd = mPosts.find(p => p.action === 'watch_add');
  ok(!!mAdd && mAdd.code === 'sh600000' && mAdd.name === '浦发银行',
     `点候选直接加入（${mAdd && mAdd.code} / ${mAdd && mAdd.name}）`);

  // 手机端保留代码方式
  mPosts.length = 0;
  mw.openWatchForm();
  await sleep(100);
  m$('fQuery').value = '600519';
  mw.submitWatch();
  await sleep(400);
  const mAdd2 = mPosts.find(p => p.action === 'watch_add');
  ok(!!mAdd2 && mAdd2.code === 'sh600519', `手机端按代码也能加（${mAdd2 && mAdd2.code}）`);

  // 手机端空输入
  mPosts.length = 0;
  mw.openWatchForm();
  await sleep(100);
  m$('fQuery').value = '';
  mw.submitWatch();
  await sleep(250);
  ok(mPosts.length === 0, '手机端空输入不会误加');

  console.log(`\n===== ${pass} 通过 / ${fail} 失败 =====`);
  process.exit(fail ? 1 : 0);
})();
