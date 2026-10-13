// KBO 기록실 수집기 (Node.js 버전) — GitHub Actions와 내 컴퓨터에서 같은 코드로 돌아가요.
//
//   node scraper/scrape.mjs                 # 올해 시즌만 다시 수집해서 data/kbo_raw.json에 합치기
//   node scraper/scrape.mjs --years 2024-2026
//   node scraper/scrape.mjs --all           # 1982년부터 전부 (약 30분)
//
// 주의: 요청은 반드시 하나씩 순서대로 보내야 해요. KBO 기록실은 서버 세션에 선택값을 저장해서,
// 동시에 여러 요청을 보내면 다른 시즌·팀 기록이 섞여 들어와요.
import * as cheerio from 'cheerio';
import fs from 'node:fs';
import path from 'node:path';

const OUT = path.resolve('data/kbo_raw.json');
const P = 'ctl00$ctl00$ctl00$cphContents$cphContents$cphContents$';
const B = 'https://www.koreabaseball.com/Record/Player/';
const REG = { hb1: 'HitterBasic/Basic1.aspx', hb2: 'HitterBasic/Basic2.aspx', run: 'Runner/Basic.aspx',
              def: 'Defense/Basic.aspx', pb1: 'PitcherBasic/Basic1.aspx', pb2: 'PitcherBasic/Basic2.aspx' };
const PS = { ph: 'HitterBasic/BasicOld.aspx', pp: 'PitcherBasic/BasicOld.aspx' };
const SERIES = ['4', '3', '5', '7'];

// ---------- args ----------
const args = process.argv.slice(2);
const thisYear = new Date(Date.now() + 9 * 3600e3).getUTCFullYear(); // KST
let years = [thisYear];
if (args.includes('--all')) years = range(1982, thisYear);
const yi = args.indexOf('--years');
if (yi >= 0) { const [a, b] = args[yi + 1].split('-').map(Number); years = range(a, b || a); }
function range(a, b) { const r = []; for (let y = a; y <= b; y++) r.push(y); return r; }

// ---------- http with a cookie jar (ASP.NET session) ----------
const jar = new Map();
const UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36';
function saveCookies(res) {
  const list = res.headers.getSetCookie ? res.headers.getSetCookie() : [];
  for (const c of list) { const [kv] = c.split(';'); const i = kv.indexOf('='); jar.set(kv.slice(0, i).trim(), kv.slice(i + 1)); }
}
const cookieHeader = () => [...jar].map(([k, v]) => `${k}=${v}`).join('; ');
async function req(url, body) {
  for (let a = 0; a < 4; a++) {
    try {
      const res = await fetch(url, {
        method: body ? 'POST' : 'GET',
        headers: { 'User-Agent': UA, 'Cookie': cookieHeader(), 'Referer': url,
                   ...(body ? { 'Content-Type': 'application/x-www-form-urlencoded' } : {}) },
        body, redirect: 'follow',
      });
      saveCookies(res);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return cheerio.load(await res.text());
    } catch (e) {
      if (a === 3) throw e;
      await sleep(2000 * (a + 1));
    }
  }
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

// ---------- page helpers (same logic as the browser version) ----------
const selv = ($, suf) => $(`select[name$="${suf}"] option[selected]`).attr('value');
function post(url, $, target, ov = {}) {
  const fd = new URLSearchParams();
  $('form').first().find('input, select').each((_, el) => {
    const e = $(el), name = e.attr('name'), type = (e.attr('type') || '').toLowerCase();
    if (!name || ['submit', 'button', 'image'].includes(type)) return;
    if ((type === 'checkbox' || type === 'radio') && e.attr('checked') === undefined) return;
    const v = el.name === 'select' ? (e.find('option[selected]').attr('value') ?? e.find('option').first().attr('value') ?? '') : (e.attr('value') ?? '');
    fd.set(name, v);
  });
  for (const k in ov) fd.set(k, ov[k]);
  fd.set('__EVENTTARGET', target); fd.set('__EVENTARGUMENT', '');
  return req(url, fd.toString());
}
function rows($) {
  const t = $('table').first();
  if (!t.length) return { head: [], rows: [] };
  const head = t.find('thead th').map((_, th) => $(th).text().trim()).get();
  const out = [];
  t.find('tbody tr').each((_, tr) => {
    const tds = $(tr).find('td').map((_, td) => $(td).text().trim()).get();
    if (tds.length <= 2) return;
    const href = $(tr).find('a[href*="playerId"]').attr('href') || '';
    out.push({ id: (href.match(/playerId=(\d+)/) || [])[1] || null, c: tds });
  });
  return { head, rows: out };
}
const pages = $ => $('.paging a[id*="btnNo"]').map((_, a) => $(a).attr('id').match(/btnNo(\d+)/)[1]).get();

async function scrape(url, season, series) {
  const base = { [P + 'ddlSeason$ddlSeason']: season, [P + 'ddlSeries$ddlSeries']: series };
  let $ = await req(url);
  $ = await post(url, $, P + 'ddlSeason$ddlSeason', base);
  if (series !== '0') $ = await post(url, $, P + 'ddlSeries$ddlSeries', base);
  if (selv($, 'ddlSeason$ddlSeason') !== season) throw new Error('season not applied');
  if (selv($, 'ddlSeries$ddlSeries') !== series) return null;
  const teams = $('select[name$="ddlTeam$ddlTeam"] option').map((_, o) => $(o).attr('value')).get().filter(Boolean);
  let head = rows($).head;
  const out = [];
  // KBO server bug: on some lists (typically the last team in the team menu) page 2+ returns the PREVIOUS
  // team's rows, however often it is requested. Wrong pages are dropped here (never mixed in); the
  // players they would have held are filled in afterwards from each player's own record page (repair()).
  const sig = r => JSON.stringify(r.rows.map(x => x.c));
  const majority = r => { const c = {}; r.rows.forEach(x => c[x.c[2]] = (c[x.c[2]] || 0) + 1); return Object.entries(c).sort((a, b) => b[1] - a[1])[0]?.[0]; };
  const seen = new Set();
  for (const tm of teams) {
    const ov = { ...base, [P + 'ddlTeam$ddlTeam']: tm };
    let t$ = null;
    for (let a = 0; a < 3 && !t$; a++) {
      const x = await post(url, $, P + 'ddlTeam$ddlTeam', ov);
      if (selv(x, 'ddlTeam$ddlTeam') === tm && selv(x, 'ddlSeries$ddlSeries') === series && selv(x, 'ddlSeason$ddlSeason') === season) t$ = x;
    }
    if (!t$) throw new Error('team filter not applied: ' + tm);
    const r1 = rows(t$);
    const teamName = majority(r1);
    const got = [r1];
    for (const p of pages(t$).slice(1)) {
      let ok = null;
      for (let a = 0; a < 2 && !ok; a++) {
        const p$ = await post(url, t$, P + 'ucPager$btnNo' + p, ov);
        const r = rows(p$);
        const hf = p$('input[name$="hfPage"]').attr('value');
        const wrong = selv(p$, 'ddlTeam$ddlTeam') !== tm || (hf && hf !== String(p)) || seen.has(sig(r)) ||
                      (r.rows.length > 1 && majority(r) !== teamName);
        if (!wrong) ok = r;
      }
      if (ok) got.push(ok);
      else warnings.push(`${season} ${url.split('/Player/')[1]} ${tm} p${p}: KBO 페이지 오류 → 이 페이지는 버리고 개인 기록으로 보충`);
    }
    for (const r of got) { if (r.rows.length) seen.add(sig(r)); if (r.head.length) head = r.head; r.rows.forEach(x => out.push([x.id, tm, ...x.c.slice(1)])); }
  }
  return { head, teams, rows: out };
}

// ---------- main ----------
const data = fs.existsSync(OUT) ? JSON.parse(fs.readFileSync(OUT, 'utf8')) : {};
const errors = [];
const warnings = [];
const jobs = [];
for (const y of years) {
  for (const k in REG) { if ((k === 'run' || k === 'def') && y < 2001) continue; jobs.push([k + y, B + REG[k], String(y), '0']); }
  for (const s of SERIES) for (const k in PS) jobs.push([k + y + '_' + s, B + PS[k], String(y), s]);
}
console.log(`수집 대상: ${years[0]}–${years.at(-1)} (${jobs.length}개 표)`);
let n = 0;
for (const [key, url, y, s] of jobs) {
  n++;
  try {
    const r = await scrape(url, y, s);
    if (r && r.rows.length) { data[key] = r; console.log(`[${n}/${jobs.length}] ${key}: ${r.rows.length}행`); }
    else { delete data[key]; console.log(`[${n}/${jobs.length}] ${key}: 기록 없음`); }
  } catch (e) {
    errors.push(`${key}: ${e.message}`);
    console.error(`[${n}/${jobs.length}] ${key}: 실패 — ${e.message} (기존 데이터 유지)`);
  }
}
// ---------- repair: fill players missing from a table using their own record page ----------
// A player who appears in one table of a season/team but not in another (dropped KBO page) gets that
// season's line from Record/Player/{Hitter|Pitcher}Detail/Total.aspx, which has no paging at all.
// Columns that page does not carry (IBB, SAC, SF, QS, NP ...) are filled with 0.
const totalCache = new Map();
const totalHead = new Map();
async function totalRows(kind, id) {
  const key = kind + id;
  if (!totalCache.has(key)) {
    const $ = await req(`https://www.koreabaseball.com/Record/Player/${kind}Detail/Total.aspx?playerId=${id}`);
    const t = $('table').first();
    const head = t.find('thead th').map((_, th) => $(th).text().trim()).get();
    totalHead.set(key, `${$('table').length}개 표, ${head.slice(0, 4).join('/')}, 제목 ${$('title').text().trim().slice(0, 20)}`);
    const list = t.find('tbody tr').map((_, tr) => [$(tr).find('td').map((_, td) => $(td).text().trim()).get()]).get()
      .filter(c => c.length === head.length).map(c => Object.fromEntries(head.map((h, i) => [h, c[i]])));
    totalCache.set(key, list);
  }
  return totalCache.get(key);
}
const n0 = v => { const x = parseFloat(String(v ?? '').replace(/,/g, '')); return isNaN(x) ? 0 : x; };
const outsOf = s => String(s || '').split(/\s+/).reduce((a, p) => a + (p.includes('/') ? Math.round(3 * (+p.split('/')[0]) / (+p.split('/')[1] || 1)) : 3 * (+p || 0)), 0);
function cellFor(col, t) {
  if (t[col] !== undefined) return t[col];
  switch (col) {
    case 'TB': return String(n0(t.H) + n0(t['2B']) + 2 * n0(t['3B']) + 3 * n0(t.HR));
    case 'OPS': return (n0(t.OBP) + n0(t.SLG)).toFixed(3);
    case 'WHIP': { const ip = outsOf(t.IP) / 3; return ip ? ((n0(t.H) + n0(t.BB)) / ip).toFixed(2) : '-'; }
    case 'MH': case 'RISP': case 'PH-BA': case 'AVG': return '-';
    default: return '0';
  }
}
async function repair(y) {
  const groups = [{ kind: 'Hitter', tables: ['hb1', 'hb2'], extra: ['run'] }, { kind: 'Pitcher', tables: ['pb1', 'pb2'], extra: [] }];
  for (const g of groups) {
    const have = {}, people = new Map();
    for (const t of [...g.tables, ...g.extra]) {
      const tab = data[t + y]; if (!tab) continue;
      have[t] = new Set(tab.rows.map(r => r[0] + '|' + r[3]));
      for (const r of tab.rows) if (r[0]) people.set(r[0] + '|' + r[3], { id: r[0], tm: r[1], name: r[2], team: r[3] });
    }
    for (const t of g.tables) {
      const tab = data[t + y]; if (!tab || !tab.head?.length) continue;
      const cols = tab.head.slice(3);
      let filled = 0;
      for (const [k, p] of people) {
        if (have[t].has(k)) continue;
        try {
          const list = await totalRows(g.kind, p.id);
          const line = list.find(r => r['연도'] === String(y) && r['팀명'] === p.team);
          if (!line) { warnings.push(`repair ${t}${y} ${p.name}(${p.id}): 개인 기록에 ${y} ${p.team} 줄 없음 (읽은 줄 ${list.length}개: ${list.slice(0, 3).map(r => r['연도'] + r['팀명']).join(',')}; 표 머리 ${totalHead.get(g.kind + p.id)})`); continue; }
          tab.rows.push([p.id, p.tm, p.name, p.team, ...cols.map(c => cellFor(c, line))]);
          have[t].add(k); filled++;
        } catch (e) { errors.push(`repair ${t}${y} ${p.name}: ${e.message}`); }
      }
      if (filled) { warnings.push(`${t}${y}: 빠진 선수 ${filled}명을 개인 기록 페이지로 보충`); console.log(`repair ${t}${y}: +${filled}`); }
    }
  }
}
for (const y of years) await repair(y);

// ---------- park factors: every regular-season result with home/away (2001+) ----------
// data["parkY"] = { teamCode: [homeG, runs in home games, awayG, runs in away games] }
for (const y of years.filter(y => y >= 2001)) {
  const T = {};
  try {
    for (let m = 3; m <= 11; m++) {
      const body = new URLSearchParams({ leId: '1', srIdList: '0', seasonId: String(y), gameMonth: String(m).padStart(2, '0'), teamId: '' });
      if (m === 3) await req('https://www.koreabaseball.com/Schedule/Schedule.aspx'); // session cookie
      const res = await fetch('https://www.koreabaseball.com/ws/Schedule.asmx/GetScheduleList', {
        method: 'POST', body: body.toString(),
        headers: { 'User-Agent': UA, 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                   'Accept': 'application/json, text/javascript, */*; q=0.01', 'X-Requested-With': 'XMLHttpRequest',
                   'Origin': 'https://www.koreabaseball.com', 'Referer': 'https://www.koreabaseball.com/Schedule/Schedule.aspx',
                   'Cookie': cookieHeader() } });
      saveCookies(res);
      const txt = await res.text();
      if (txt.trimStart()[0] !== '{') throw new Error('schedule service returned HTML (month ' + m + ')');
      const j = JSON.parse(txt);
      for (const R of j.rows || []) {
        const cells = R.row, play = cells.find(c => c.Class === 'play');
        const gid = (cells.map(c => c.Text || '').join(' ').match(/gameId=(\d{8}[A-Z]{2}[A-Z]{2}\d)/) || [])[1];
        if (!play || !gid) continue;
        const sp = cheerio.load(play.Text)('em span').map((_, e) => cheerio.load(e).text().trim()).get();
        if (sp.length < 3 || sp[0] === '' || sp[2] === '') continue;
        const a = +sp[0], h = +sp[2]; if (isNaN(a) || isNaN(h)) continue;
        const aw = gid.slice(8, 10), hm = gid.slice(10, 12);
        T[hm] ??= [0, 0, 0, 0]; T[aw] ??= [0, 0, 0, 0];
        T[hm][0]++; T[hm][1] += a + h; T[aw][2]++; T[aw][3] += a + h;
      }
    }
    if (Object.keys(T).length) { data['park' + y] = T; console.log(`park${y}: ${Object.keys(T).length}개 팀`); }
  } catch (e) { errors.push(`park${y}: ${e.message}`); }
}

data._meta = { scrapedAt: new Date().toISOString(), errors, warnings };
fs.mkdirSync(path.dirname(OUT), { recursive: true });
fs.writeFileSync(OUT, JSON.stringify(data));
console.log(`저장: ${OUT} · 실패 ${errors.length}개`);
if (errors.length && errors.length === jobs.length) process.exit(1); // 전부 실패하면 Actions를 빨간불로
