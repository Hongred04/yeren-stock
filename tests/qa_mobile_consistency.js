// QA 独立验证：mobile.html 在真实新夹具下的同屏文案自洽（DOM 级）
// 复现用户 10-12 截图场景的三处：副标题 / 打法框 / 温度计副标题
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'mobile.html'), 'utf8');
const report = JSON.parse(fs.readFileSync(path.join(__dirname, '_test_report.json'), 'utf8'));

let pass = 0, fail = 0;
const ok = (c, m) => { c ? pass++ : fail++; console.log((c ? '  ✅ ' : '  ❌ ') + m); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const dom = new JSDOM(html, {
    runScripts: 'dangerously', pretendToBeVisual: true,
    url: 'http://127.0.0.1:8777/mobile.html',
  });
  const w = dom.window;
  w.fetch = async (u) => {
    const url = String(u);
    let body = {};
    if (url.includes('/api/result')) body = report;
    else if (url.includes('/api/status')) body = { done: true, pct: 100, msg: '完成' };
    return { ok: true, status: 200, json: async () => body };
  };
  w.scrollTo = () => {};

  await sleep(5000);

  const today = w.document.getElementById('tab-today');
  const txt = today.textContent;

  // ── ① 副标题（.sub）：只拼状态，不含选股口径/行动指令 ──
  const sub = today.querySelector('.sub');
  ok(!!sub, '副标题节点存在');
  const subTxt = sub ? sub.textContent : '';
  console.log('    副标题实际渲染：' + subTxt);
  const badWords = ['波段', '短线', '大票', '空仓', '回避', '低吸'];
  const hits = badWords.filter(x => subTxt.includes(x));
  ok(hits.length === 0, `副标题不含口径/指令词（命中: ${hits.join(',') || '无'}）`);

  // ── ② 打法框：必须叫「今日应对打法」 ──
  const modeBox = today.querySelector('.mode');
  ok(!!modeBox, '打法框节点存在');
  const modeTxt = modeBox ? modeBox.textContent : '';
  console.log('    打法框实际渲染：' + modeTxt.slice(0, 80));
  ok(modeTxt.includes('今日应对打法'), '打法框标题为「今日应对打法」');
  ok(!modeTxt.includes('当前打法'), '旧文案「当前打法」已不存在');

  // ── ③ 温度计三格：60° 时副标题必须是温度档位名 ──
  const rep = report.report || report;
  const meter = rep.meter || {};
  const statBoxes = today.querySelectorAll('.stats3 .stat');
  let tempSub = '';
  statBoxes.forEach(b => {
    if (b.querySelector('.t') && b.querySelector('.t').textContent === '情绪温度') {
      tempSub = (b.querySelector('.s') || {}).textContent || '';
    }
  });
  console.log('    温度格副标题实际渲染：' + tempSub);
  const STAGE_WORDS = ['修复·启动', '发酵', '高潮', '冰点', '退潮'];
  const inStage = STAGE_WORDS.some(x => tempSub.includes(x));
  ok(inStage, `温度计副标题是温度档位名（${tempSub}）`);

  // ── ④ 同屏自洽：M3 与 E1 的 ebb 必须同向出现 ──
  const sen = rep.sentiment || {};
  const m3Ebb = sen.stage === 'ebb';
  const e1Ebb = meter.stage === 'ebb';
  ok(m3Ebb === e1Ebb, `M3(${sen.stage}) 与 E1(${meter.stage}) 的 ebb 判定同向`);
  if (m3Ebb) {
    // M3 确认退潮 → 温度计可报退潮、结论卡必须同向（防守语义）
    const head = ((rep.outlook || {}).headline) || '';
    ok(/不要买|防守|空仓|退潮|清仓/.test(head + modeTxt),
       `结论卡同向（headline: ${head}）`);
  }

  // ── ⑤ 无 undefined/NaN ──
  ok(!/undefined|NaN/.test(txt), '今日 Tab 无 undefined/NaN');

  console.log(`\n===== ${pass} 通过 / ${fail} 失败 =====`);
  process.exit(fail ? 1 : 0);
})();
