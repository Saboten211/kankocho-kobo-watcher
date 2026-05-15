#!/usr/bin/env python3
"""観光庁 公募ウォッチャー — 取得・差分検知・静的サイト生成を1ファイルで行う。

実行すると data.json（スナップショット兼差分の元）と index.html（自己完結
ダッシュボード）を出力する。1日1回の実行を想定。GitHub Actions でもローカル
cron でも同じ。詳細は SPEC.md。
"""
import json
import logging
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://www.mlit.go.jp"
HUB_URL = BASE + "/kankocho/kobo.html"
BOSHU_URL = BASE + "/kankocho/kobo_boshu.html"
USER_AGENT = "kankocho-kobo-watcher/1.0 (internal use; once per day)"
REQUEST_TIMEOUT = 30
POLITE_DELAY_SEC = 1.0  # 詳細ページ連続取得の間隔（礼儀）
NEW_BADGE_DAYS = 7      # first_seen がこの日数以内なら NEW 表示
DL_PARSER_VERSION = 3   # parse_deadline 改修時に+1。古い版のキャッシュを再取得させる

HERE = Path(__file__).parent
DATA_PATH = HERE / "data.json"
SITE_PATH = HERE / "index.html"
LOG_PATH = HERE / "run.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_PATH, encoding="utf-8")],
)
log = logging.getLogger("kobo")


def fetch(url: str) -> str:
    headers = {"User-Agent": USER_AGENT}
    last_err = None
    for attempt in (1, 2):
        try:
            r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            r.encoding = "utf-8"
            return r.text
        except requests.RequestException as e:
            last_err = e
            log.warning("取得失敗(%d回目) %s: %s", attempt, url, e)
            time.sleep(2)
    raise RuntimeError(f"取得不能: {url}: {last_err}")


def parse_jp_date(text: str) -> str | None:
    """「2026年5月15日」→ 「2026-05-15」。取れなければ None。"""
    m = re.search(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日", text)
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def resolve_year_list_url(hub_html: str, year: int) -> str | None:
    """ハブ kobo.html から当年の年別一覧URLを解決する。
    年別URLは kobo_2026_00003.html のように年・連番が入りハードコード不可。
    テキストが「{year}年」かつ href が /kankocho/kobo_ で始まる .html を採用。
    """
    soup = BeautifulSoup(hub_html, "html.parser")
    want = f"{year}年"
    for a in soup.find_all("a", href=True):
        if a.get_text(strip=True) == want and re.search(r"/kankocho/kobo_.*\.html$", a["href"]):
            return urljoin(BASE, a["href"])
    return None


def parse_list(html: str) -> list[dict]:
    """一覧ページ（募集中・年別とも同一構造）から案件を抽出。"""
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict] = []
    for ul in soup.select("ul.st-news-list"):
        for li in ul.find_all("li", recursive=False):
            a = li.find("a", href=True)
            if not a:
                continue
            url = urljoin(BASE, a["href"])
            date_el = li.select_one(".st-news-list__date")
            tag_el = li.select_one(".st-news-list__tag")
            txt_el = li.select_one(".st-news-list__txt p") or li.select_one(".st-news-list__txt")
            cats = [s.get_text(strip=True) for s in li.select(".st-news-list__icon-item")]
            date_raw = date_el.get_text(strip=True) if date_el else ""
            items.append({
                "url": url,
                "title": txt_el.get_text(strip=True) if txt_el else a.get_text(strip=True),
                "status": tag_el.get_text(strip=True) if tag_el else "",
                "date_raw": date_raw,
                "date": parse_jp_date(date_raw),
                "categories": cats,
            })
    return items


_D = r"[0-9０-９]"
DEADLINE_HINT = re.compile(r"(締切|締め切り|応募期限|募集期間|応募期間|受付期間|公募期間|提出期限|提出期間)")
# 実日付（月日 / 年月 / 時刻）を含む行だけを締切値として採用する。
# これにより「…申請受付期間のお知らせ」のような見出しを除外できる。
STRICT_DATE = re.compile(
    rf"({_D}{{1,2}}\s*月\s*{_D}{{1,2}}\s*日|{_D}{{4}}\s*年\s*{_D}{{1,2}}\s*月|{_D}{{1,2}}\s*[:：]\s*{_D}{{2}})"
)
DATEISH = re.compile(rf"(令和|平成|{_D}{{1,2}}\s*月|～|〜)")
NOT_DEADLINE = re.compile(r"(最終更新|掲載日|更新日)")


def parse_deadline(detail_html: str) -> str | None:
    """詳細ページから締切/募集期間らしき行を原文のまま1つ拾う。
    自由文なので構造化はしない（SPEC: v1の判断）。
    観光庁の詳細ページは「公募期間」ラベルと日付が別行になるレイアウトが
    あるため、ヒント行に日付が無ければ直後の日付行を値として採用する。
    """
    soup = BeautifulSoup(detail_html, "html.parser")
    # title/nav/breadcrumb 等を除外（パンくず "… | 公募情報 | 観光庁" の誤検出防止）
    for tag in soup(["title", "script", "style", "noscript", "nav", "header", "footer"]):
        tag.decompose()
    lines = [l.strip() for l in soup.get_text("\n", strip=True).split("\n") if l.strip()]
    lines = [l for l in lines if not ("公募情報" in l and "観光庁" in l)]

    def with_trailing(line: str, i: int) -> str:
        # 行末が「令和/平成」で切れている場合は次の日付行を連結
        if re.search(r"(令和|平成)\s*$", line):
            for nxt in lines[i + 1:i + 3]:
                if DATEISH.search(nxt):
                    return line + nxt
        return line

    for i, line in enumerate(lines):
        if not DEADLINE_HINT.search(line):
            continue
        if STRICT_DATE.search(line) and not NOT_DEADLINE.search(line) and len(line) <= 140:
            return with_trailing(line, i)
        label = re.sub(r"[\s:：・]+$", "", line)
        for j, nxt in enumerate(lines[i + 1:i + 5], start=i + 1):
            if STRICT_DATE.search(nxt) and not NOT_DEADLINE.search(nxt) and len(nxt) <= 140:
                val = with_trailing(nxt, j)
                return f"{label}: {val}" if len(label) <= 12 else val
    return None


_Z2H = str.maketrans("０１２３４５６７８９：", "0123456789:")
_ERA_BASE = {"令和": 2018, "令": 2018, "R": 2018, "平成": 1988, "平": 1988, "H": 1988}
_DATE_RE = re.compile(
    r"(?:(令和|平成|R|H)\s*(\d{1,2})\s*年|(\d{4})\s*年)?\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
)
_RANGE_CHARS = ("～", "〜", "~", "−", "ー", "-")


def extract_deadline_date(text: str | None) -> str | None:
    """締切テキストから期間末日を ISO 日付で取り出す。和暦を西暦に変換。
    範囲の2つ目の日付が「年」を省く（…令和8年5月29日～6月18日）ため、
    直前に出た年を引き継ぐ。開始日しか書かれていない場合は None。
    """
    if not text:
        return None
    t = text.translate(_Z2H)
    dates: list[date] = []
    cur_year: int | None = None
    for m in _DATE_RE.finditer(t):
        era, ey, wy, mo, da = m.groups()
        if wy:
            y = int(wy)
        elif era and ey:
            y = _ERA_BASE[era] + int(ey)
        else:
            y = cur_year
        if y is None:
            continue
        cur_year = y
        try:
            dates.append(date(y, int(mo), int(da)))
        except ValueError:
            continue
    if not dates:
        return None
    is_range = any(c in t for c in _RANGE_CHARS)
    has_close = re.search(r"(締切|締め切り|必着|まで|期限|〆)", t)
    if len(dates) == 1 and not is_range and not has_close and "開始" in t:
        return None  # 公募開始日だけ → 締切不明
    return max(dates).isoformat()


def load_prev() -> dict:
    if DATA_PATH.exists():
        try:
            return json.loads(DATA_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("data.json が壊れている。空として扱う")
    return {}


def main() -> int:
    today = date.today().isoformat()
    year = date.today().year

    hub_html = fetch(HUB_URL)
    year_list_url = resolve_year_list_url(hub_html, year)
    if not year_list_url:
        log.error("当年(%d)の年別一覧URLを解決できなかった。サイト改修の可能性。中断", year)
        return 2
    log.info("当年一覧URL: %s", year_list_url)

    year_items = parse_list(fetch(year_list_url))
    time.sleep(POLITE_DELAY_SEC)
    boshu_items = parse_list(fetch(BOSHU_URL))
    boshu_urls = {it["url"] for it in boshu_items}
    log.info("年別一覧 %d件 / 募集中一覧 %d件", len(year_items), len(boshu_items))

    # 年別一覧を母集合に。募集中一覧にあるものは現在募集中とみなす。
    merged: dict[str, dict] = {}
    for it in year_items + boshu_items:
        cur = merged.get(it["url"])
        if cur is None:
            merged[it["url"]] = dict(it)
        else:
            # カテゴリ等は情報量の多い方を残す
            if len(it["categories"]) > len(cur["categories"]):
                cur["categories"] = it["categories"]
    for url, it in merged.items():
        it["open"] = (url in boshu_urls) or (it["status"] == "募集中")

    prev = load_prev()
    prev_records = {r["url"]: r for r in prev.get("records", [])}
    prev_count = len(prev_records)

    # サニティガード: 0件 or 前回比で半減 → サイト改修等の異常。上書きせず終了。
    if len(merged) == 0 or (prev_count and len(merged) < prev_count * 0.5):
        log.error("件数異常(今回%d / 前回%d)。パース崩壊の疑い。data.json は更新しない",
                  len(merged), prev_count)
        return 3

    # 詳細から締切テキスト取得。負荷を抑えるため「募集中 かつ 未キャッシュ/状態変化」のみ。
    fetched = 0
    for url, it in merged.items():
        prev_rec = prev_records.get(url)
        reuse = (
            prev_rec
            and prev_rec.get("status") == it["status"]
            and "deadline_text" in prev_rec
            and prev_rec.get("_dl_ver") == DL_PARSER_VERSION
        )
        if reuse:
            it["deadline_text"] = prev_rec.get("deadline_text")
            it["_dl_ver"] = prev_rec.get("_dl_ver")
        elif it["open"]:
            try:
                it["deadline_text"] = parse_deadline(fetch(url))
                it["_dl_ver"] = DL_PARSER_VERSION
            except RuntimeError as e:
                log.warning("詳細取得スキップ %s: %s", url, e)
                it["deadline_text"] = prev_rec.get("deadline_text") if prev_rec else None
                it["_dl_ver"] = prev_rec.get("_dl_ver") if prev_rec else None
            fetched += 1
            time.sleep(POLITE_DELAY_SEC)
        else:
            it["deadline_text"] = prev_rec.get("deadline_text") if prev_rec else None
            it["_dl_ver"] = prev_rec.get("_dl_ver") if prev_rec else None
    log.info("詳細ページ取得 %d件", fetched)

    # 差分: first_seen は監査用に保持。NEW は観光庁の公開日が直近 N 日かで判定
    # （first_seen 基準だと初回デプロイ時に全件 NEW になり意味をなさないため）。
    new_cnt = changed_cnt = due_soon_cnt = 0
    for url, it in merged.items():
        prev_rec = prev_records.get(url)
        it["first_seen"] = prev_rec.get("first_seen", today) if prev_rec else today
        ref = it.get("date") or it["first_seen"]
        try:
            age = (date.today() - date.fromisoformat(ref)).days
        except (ValueError, TypeError):
            age = 999
        it["is_new"] = 0 <= age <= NEW_BADGE_DAYS
        if it["is_new"]:
            new_cnt += 1
        it["deadline_date"] = extract_deadline_date(it.get("deadline_text"))
        if it["deadline_date"]:
            it["days_left"] = (date.fromisoformat(it["deadline_date"]) - date.today()).days
            if it["open"] and 0 <= it["days_left"] <= NEW_BADGE_DAYS:
                due_soon_cnt += 1
        else:
            it["days_left"] = None
        if prev_rec and prev_rec.get("status") and prev_rec["status"] != it["status"]:
            it["status_changed"] = {"from": prev_rec["status"], "to": it["status"]}
            changed_cnt += 1

    records = sorted(merged.values(),
                     key=lambda r: (r["date"] or "0000-00-00", r["title"]), reverse=True)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": {"hub": HUB_URL, "year_list": year_list_url, "boshu": BOSHU_URL},
        "counts": {"total": len(records),
                   "open": sum(1 for r in records if r["open"]),
                   "new": new_cnt, "due_soon": due_soon_cnt,
                   "status_changed": changed_cnt},
        "records": records,
    }
    DATA_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    SITE_PATH.write_text(render_site(payload), encoding="utf-8")
    log.info("完了: 全%d件 / 募集中%d / 新着%d / 状態変化%d → data.json, index.html 更新",
             len(records), payload["counts"]["open"], new_cnt, changed_cnt)
    return 0


def render_site(payload: dict) -> str:
    data_json = json.dumps(payload, ensure_ascii=False)
    return SITE_TEMPLATE.replace("/*__DATA__*/null", data_json)


SITE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>観光庁 公募ウォッチャー</title>
<style>
  :root{
    --teal:#00738A; --teal-l:#3AABD2; --navy:#17406D;
    --pink:#E71C57; --ink:#1a1a1a; --sub:#575757; --line:#e3e3e3; --bg:#f7f8f9;
  }
  *{box-sizing:border-box}
  body{margin:0;font-family:"Hiragino Kaku Gothic ProN","Meiryo",sans-serif;
    color:var(--ink);background:var(--bg);line-height:1.6}
  header{background:#fff;border-bottom:3px solid var(--teal);padding:20px 24px}
  header h1{margin:0;font-size:20px;color:var(--teal)}
  header .meta{margin-top:4px;font-size:12px;color:var(--sub)}
  .wrap{max-width:1040px;margin:0 auto;padding:20px 24px 60px}
  .stats{display:flex;gap:18px;flex-wrap:wrap;margin:14px 0 18px}
  .stat{background:#fff;border:1px solid var(--line);border-radius:8px;
    padding:10px 16px;min-width:96px}
  .stat b{display:block;font-size:22px;color:var(--teal)}
  .stat span{font-size:12px;color:var(--sub)}
  .controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center;
    background:#fff;border:1px solid var(--line);border-radius:8px;padding:12px;margin-bottom:16px}
  .controls input,.controls select{font-size:14px;padding:7px 10px;
    border:1px solid var(--line);border-radius:6px}
  .controls input{flex:1;min-width:200px}
  .pill{display:inline-block;font-size:11px;font-weight:bold;border-radius:999px;
    padding:2px 9px;color:#fff;white-space:nowrap}
  .p-open{background:var(--teal)}
  .p-end{background:#9aa0a6}
  .p-result{background:var(--navy)}
  .p-new{background:var(--pink)}
  .p-chg{background:#b8860b}
  .p-due-urgent{background:var(--pink)}
  .p-due-warn{background:#c47f00}
  .p-due-ok{background:#5a8f3c}
  .p-due-over{background:#9aa0a6}
  ul.list{list-style:none;margin:0;padding:0}
  li.card{background:#fff;border:1px solid var(--line);border-radius:8px;
    padding:14px 16px;margin-bottom:10px}
  li.card .top{display:flex;gap:8px;align-items:center;flex-wrap:wrap;
    font-size:12px;color:var(--sub)}
  li.card a.title{display:block;margin:6px 0;font-size:15px;font-weight:bold;
    color:var(--navy);text-decoration:none}
  li.card a.title:hover{text-decoration:underline}
  li.card .dl{font-size:13px;color:var(--ink);background:#fff7e6;
    border:1px solid #f0d9a8;border-radius:6px;padding:5px 9px;margin:4px 0}
  li.card .cats{margin-top:6px}
  li.card .cat{display:inline-block;font-size:11px;color:var(--sub);
    border:1px solid var(--line);border-radius:4px;padding:1px 7px;margin:0 4px 4px 0}
  .empty{color:var(--sub);text-align:center;padding:40px}
  footer{font-size:11px;color:var(--sub);text-align:center;padding:20px}
  footer a{color:var(--teal)}
</style>
</head>
<body>
<header>
  <h1>観光庁 公募ウォッチャー</h1>
  <div class="meta" id="meta"></div>
</header>
<div class="wrap">
  <div class="stats" id="stats"></div>
  <div class="controls">
    <input id="q" type="search" placeholder="事業名で絞り込み">
    <select id="status">
      <option value="">ステータス：すべて</option>
      <option value="open">募集中のみ</option>
      <option value="募集終了">募集終了</option>
      <option value="採択結果">採択結果</option>
    </select>
    <select id="cat"><option value="">カテゴリ：すべて</option></select>
    <select id="sort">
      <option value="date">並び：公開日が新しい順</option>
      <option value="deadline">並び：締切が近い順</option>
    </select>
    <label style="font-size:13px;color:var(--sub)">
      <input type="checkbox" id="newonly"> 新着のみ</label>
  </div>
  <ul class="list" id="list"></ul>
  <div class="empty" id="empty" style="display:none">該当する公募はありません</div>
</div>
<footer>
  出典：観光庁 公募情報 ／ このページは自動生成（手動の最終確認は各詳細ページで）<br>
  <span id="src"></span>
</footer>
<script>
const PAYLOAD = /*__DATA__*/null;
const R = PAYLOAD.records;
const $ = s => document.querySelector(s);

document.getElementById('meta').textContent =
  '最終更新: ' + PAYLOAD.generated_at.replace('T',' ');
const c = PAYLOAD.counts;
document.getElementById('stats').innerHTML = [
  ['全公募', c.total], ['募集中', c.open], ['締切間近(7日内)', c.due_soon ?? 0],
  ['新着(公開7日内)', c.new], ['状態変化', c.status_changed]
].map(([k,v])=>`<div class="stat"><b>${v}</b><span>${k}</span></div>`).join('');
document.getElementById('src').innerHTML =
  ['hub','year_list','boshu'].map(k=>`<a href="${PAYLOAD.source[k]}" target="_blank" rel="noopener">${k}</a>`).join(' / ');

const cats = [...new Set(R.flatMap(r=>r.categories||[]))].sort();
const catSel = document.getElementById('cat');
cats.forEach(x=>{const o=document.createElement('option');o.value=x;o.textContent='カテゴリ：'+x;catSel.appendChild(o)});

function statusPill(r){
  if(r.open) return '<span class="pill p-open">募集中</span>';
  if(r.status.indexOf('採択')>=0) return '<span class="pill p-result">採択結果</span>';
  return '<span class="pill p-end">'+(r.status||'募集終了')+'</span>';
}
function esc(s){return (s||'').replace(/[&<>"]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]))}
function dueChip(r){
  if(!r.deadline_date) return '';
  const md=r.deadline_date.slice(5).replace('-','/').replace(/^0/,'');
  const d=r.days_left;
  let cls='p-due-ok', label='あと'+d+'日';
  if(d<0){cls='p-due-over';label='締切終了';}
  else if(d===0){cls='p-due-urgent';label='本日締切';}
  else if(d<=7){cls='p-due-urgent';}
  else if(d<=21){cls='p-due-warn';}
  return `<span class="pill ${cls}">締切 ${md}・${label}</span>`;
}

function render(){
  const q=$('#q').value.trim();
  const st=$('#status').value, ct=$('#cat').value, no=$('#newonly').checked;
  const sort=$('#sort').value;
  let rows=R.filter(r=>{
    if(q && r.title.indexOf(q)<0) return false;
    if(st==='open' && !r.open) return false;
    if(st && st!=='open' && (r.status||'').indexOf(st)<0) return false;
    if(ct && !(r.categories||[]).includes(ct)) return false;
    if(no && !r.is_new) return false;
    return true;
  });
  if(sort==='deadline'){
    rows=rows.slice().sort((a,b)=>{
      const ka=a.deadline_date||'9999-12-31', kb=b.deadline_date||'9999-12-31';
      return ka<kb?-1:ka>kb?1:0;
    });
  }
  const list=$('#list');
  list.innerHTML=rows.map(r=>`
    <li class="card">
      <div class="top">
        ${statusPill(r)}
        ${dueChip(r)}
        ${r.is_new?'<span class="pill p-new">NEW</span>':''}
        ${r.status_changed?`<span class="pill p-chg">${esc(r.status_changed.from)}→${esc(r.status_changed.to)}</span>`:''}
        <span>公開日 ${esc(r.date_raw||r.date||'-')}</span>
      </div>
      <a class="title" href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title)}</a>
      ${r.deadline_text?`<div class="dl">⏰ ${esc(r.deadline_text)}</div>`:''}
      <div class="cats">${(r.categories||[]).map(x=>`<span class="cat">${esc(x)}</span>`).join('')}</div>
    </li>`).join('');
  $('#empty').style.display = rows.length? 'none':'block';
}
['q','status','cat','sort','newonly'].forEach(id=>{
  const el=document.getElementById(id);
  el.addEventListener(id==='q'?'input':'change',render);
});
render();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # 予期しない失敗は data.json を温存して非0終了
        log.exception("想定外エラー: %s", e)
        sys.exit(1)
