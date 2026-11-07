"""產生自包含的 index.html 儀表板（純前端，不需伺服器）。"""
from __future__ import annotations

import html
import json
from datetime import date, datetime

STATUS_ORDER = {"進行中": 0, "即將開始": 1, "已截止": 2, "日期未識別": 3}


def compute_status(start: date | None, end: date | None, today: date) -> str:
    # 只有截止日（多半是「報名至 X 日止」）：仍可判斷是否已截止
    if not start and end:
        return "已截止" if today > end else "進行中"
    # 只有開始日（多半是「報名開放日 X」）：只能判斷是否還沒開始
    if start and not end:
        return "即將開始" if today < start else "進行中"
    if not start or not end:
        return "日期未識別"
    if today < start:
        return "即將開始"
    if today > end:
        return "已截止"
    return "進行中"


def _fmt(d) -> str:
    if not d:
        return ""
    if isinstance(d, str):
        return d
    return d.isoformat()


def roc(d) -> str:
    """西元日期轉民國顯示，例如 2024-03-01 → 113/03/01"""
    if not d:
        return ""
    if isinstance(d, str):
        try:
            d = date.fromisoformat(d)
        except ValueError:
            return ""
    return f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"


def build_rows(db_rows: list[dict], today: date) -> list[dict]:
    out = []
    for r in db_rows:
        start = date.fromisoformat(r["start_date"]) if r.get("start_date") else None
        end = date.fromisoformat(r["end_date"]) if r.get("end_date") else None
        status = compute_status(start, end, today)
        conf = float(r.get("confidence") or 0)
        out.append(
            {
                "school": r["school"],
                "source_id": r["source_id"],
                "title": r["title"],
                "url": r["url"],
                "published": r.get("published") or "",
                "start": _fmt(start),
                "end": _fmt(end),
                "start_roc": roc(start),
                "end_roc": roc(end),
                "status": status,
                "evidence": r.get("evidence") or "",
                "confidence": conf,
                "rule": r.get("rule") or "",
                "needs_review": bool(status == "日期未識別" or conf < 0.45),
                "first_seen": (r.get("first_seen") or "")[:16].replace("T", " "),
                "last_seen": (r.get("last_seen") or "")[:16].replace("T", " "),
                "attachments": r.get("attachments") or [],
            }
        )
    out.sort(key=lambda x: (STATUS_ORDER.get(x["status"], 9), x["end"] or "9999-12-31", x["published"]))
    return out


TEMPLATE = """<!DOCTYPE html>
<html lang="zh-Hant-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>臺北市國小課後社團公告監測</title>
<style>
:root{
  --bg:#f5f7fa; --card:#ffffff; --ink:#16212e; --muted:#65758a; --line:#e3e8ef;
  --brand:#1d4ed8; --ok:#0f766e; --warn:#b45309; --dead:#6b7280; --danger:#b91c1c;
  --shadow:0 1px 2px rgba(16,24,40,.06),0 1px 3px rgba(16,24,40,.1);
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:"Noto Sans TC","PingFang TC","Microsoft JhengHei",system-ui,-apple-system,sans-serif;
  font-size:14px;line-height:1.6}
.wrap{max-width:1280px;margin:0 auto;padding:28px 20px 60px}
header{display:flex;flex-wrap:wrap;gap:16px;align-items:flex-end;justify-content:space-between;margin-bottom:18px}
h1{font-size:26px;margin:0;letter-spacing:.5px}
.sub{color:var(--muted);font-size:13px;margin-top:6px}
.note{background:#fff7ed;border:1px solid #fed7aa;color:#7c2d12;padding:10px 14px;border-radius:10px;font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:18px 0 22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;box-shadow:var(--shadow)}
.card .k{color:var(--muted);font-size:12px}
.card .v{font-size:26px;font-weight:700;margin-top:2px;font-variant-numeric:tabular-nums}
.card.ok .v{color:var(--ok)} .card.warn .v{color:var(--warn)} .card.dead .v{color:var(--dead)}
.card.danger .v{color:var(--danger)}
.toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;background:var(--card);
  border:1px solid var(--line);border-radius:12px;padding:12px;box-shadow:var(--shadow);margin-bottom:14px}
input[type=search],select{border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:14px;
  background:#fff;color:var(--ink);outline:none}
input[type=search]{min-width:260px;flex:1}
input[type=search]:focus,select:focus{border-color:var(--brand);box-shadow:0 0 0 3px rgba(29,78,216,.12)}
.chk{display:inline-flex;align-items:center;gap:6px;color:var(--muted);cursor:pointer}
table{width:100%;border-collapse:separate;border-spacing:0;background:var(--card);
  border:1px solid var(--line);border-radius:12px;overflow:hidden;box-shadow:var(--shadow)}
thead th{background:#f8fafc;text-align:left;font-size:12.5px;color:var(--muted);font-weight:600;
  padding:11px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
tbody td{padding:12px;border-bottom:1px solid #eef2f7;vertical-align:top}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover{background:#fafcff}
.school{font-weight:600;white-space:nowrap}
.title a{color:var(--brand);text-decoration:none;font-weight:600}
.title a:hover{text-decoration:underline}
.att a{display:inline-block;margin:2px 6px 2px 0;font-size:12px;color:#0f766e;text-decoration:none;
  border:1px solid #ccfbf1;background:#f0fdfa;border-radius:6px;padding:1px 6px}
.att a:hover{background:#ccfbf1}
.d{font-variant-numeric:tabular-nums;white-space:nowrap}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap}
.b-live{background:#ecfdf5;color:#047857;border:1px solid #a7f3d0}
.b-soon{background:#fffbeb;color:#b45309;border:1px solid #fde68a}
.b-dead{background:#f3f4f6;color:#4b5563;border:1px solid #e5e7eb}
.b-none{background:#fef2f2;color:#b91c1c;border:1px solid #fecaca}
.review{display:inline-block;margin-left:6px;padding:1px 7px;border-radius:999px;font-size:11px;
  background:#fff1f2;color:#be123c;border:1px solid #fecdd3;white-space:nowrap}
.evidence{color:var(--muted);font-size:12.5px;margin-top:4px;display:none;white-space:pre-wrap}
tr.open .evidence{display:block}
.frag-toggle{background:none;border:none;color:var(--muted);cursor:pointer;font-size:12px;padding:0;margin-top:4px}
.frag-toggle:hover{color:var(--brand)}
.empty{padding:40px;text-align:center;color:var(--muted)}
footer{margin-top:26px;color:var(--muted);font-size:12.5px;line-height:1.9}
.srclist{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.tag{padding:2px 9px;border-radius:6px;font-size:12px;border:1px solid var(--line);background:#fff}
.tag.ok{color:#047857;border-color:#a7f3d0;background:#ecfdf5}
.tag.bad{color:#b91c1c;border-color:#fecaca;background:#fef2f2}
.tag.skip{color:#6b7280;background:#f9fafb}
@media(max-width:820px){
  thead{display:none}
  table,tbody,tr,td{display:block;width:100%}
  tbody td{border-bottom:none;padding:6px 12px}
  tr{border-bottom:1px solid var(--line);padding:8px 0}
  .cell-label{color:var(--muted);font-size:11.5px;display:block}
}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>臺北市國小課後社團公告監測</h1>
      <div class="sub">掃描臺北市教育局與各國小公告，抽取「申請開始／結束日期」。資料為規則式抽取，<b>需人工複核</b>。</div>
    </div>
    <div class="note">產生時間：__GENERATED__　·　資料筆數：__TOTAL__　·　最後執行：__LASTRUN__</div>
  </header>

  <div class="cards" id="cards"></div>

  <div class="toolbar">
    <input type="search" id="q" placeholder="搜尋學校、公告標題、原文片段…">
    <select id="status">
      <option value="">全部狀態</option>
      <option value="進行中">進行中</option>
      <option value="即將開始">即將開始</option>
      <option value="已截止">已截止</option>
      <option value="日期未識別">日期未識別</option>
    </select>
    <select id="sort">
      <option value="end_asc">依申請結束日（近→遠）</option>
      <option value="end_desc">依申請結束日（遠→近）</option>
      <option value="start_asc">依申請開始日</option>
      <option value="pub_desc">依公告日期（新→舊）</option>
      <option value="school">依學校名稱</option>
    </select>
    <label class="chk"><input type="checkbox" id="review"> 只看需人工複核</label>
    <span id="count" class="sub" style="margin-left:auto"></span>
  </div>

  <table id="tbl">
    <thead>
      <tr>
        <th style="width:150px">學校</th>
        <th>公告標題</th>
        <th style="width:104px">公告日期</th>
        <th style="width:104px">申請開始</th>
        <th style="width:104px">申請結束</th>
        <th style="width:104px">狀態</th>
        <th style="width:120px">附件</th>
      </tr>
    </thead>
    <tbody id="tbody"></tbody>
  </table>
  <div id="empty" class="empty" style="display:none">沒有符合條件的公告</div>

  <footer>
    <div><b>資料來源</b></div>
    <div class="srclist" id="srclist"></div>
    <div style="margin-top:14px">
      <b>重要限制</b>：本站以關鍵字掃描公開公告，無法保證涵蓋所有學校或所有公告；
      日期由規則式啟發法抽取（支援民國年與西元年、單一日期區間、只寫一次年份等情況），
      可能誤抓活動日期或漏抓附件內表格。每筆資料都保留「原文片段」，請務必點開核對後再使用。
      狀態依申請結束日自動推算（今日：__TODAY__）。
    </div>
  </footer>
</div>

<script id="data" type="application/json">__DATA__</script>
<script>
(function(){
  const DATA = JSON.parse(document.getElementById('data').textContent);
  const ROWS = DATA.rows, SOURCES = DATA.sources, RUN = DATA.run || {};
  const tb = document.getElementById('tbody');
  const esc = s => (s||'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  const STATUS_CLASS = {'進行中':'b-live','即將開始':'b-soon','已截止':'b-dead','日期未識別':'b-none'};

  function cards(){
    const c = {'進行中':0,'即將開始':0,'已截止':0,'日期未識別':0};
    ROWS.forEach(r => c[r.status] = (c[r.status]||0)+1);
    const review = ROWS.filter(r=>r.needs_review).length;
    const items = [
      {k:'公告總數', v:ROWS.length, cls:''},
      {k:'進行中', v:c['進行中'], cls:'ok'},
      {k:'即將開始', v:c['即將開始'], cls:'warn'},
      {k:'已截止', v:c['已截止'], cls:'dead'},
      {k:'日期未識別', v:c['日期未識別'], cls:'danger'},
      {k:'需人工複核', v:review, cls:'danger'}
    ];
    document.getElementById('cards').innerHTML = items.map(i =>
      `<div class="card ${i.cls}"><div class="k">${i.k}</div><div class="v">${i.v}</div></div>`).join('');
  }

  function sourceTags(){
    const counts = {};
    ROWS.forEach(r => counts[r.source_id] = (counts[r.source_id]||0)+1);
    document.getElementById('srclist').innerHTML = SOURCES.map(s => {
      const cls = s.status === 'ok' ? 'ok' : (s.status === 'disabled' ? 'skip' : 'bad');
      const cnt = counts[s.id] || 0;
      const label = s.status === 'disabled' ? '已停用' : (s.status === 'error' ? '失敗' : `${cnt} 筆`);
      const tl = s.error ? ` title="${esc(s.error)}"` : '';
      return `<span class="tag ${cls}"${tl}>${esc(s.name)} · ${label}</span>`;
    }).join('');
  }

  function render(){
    const q = document.getElementById('q').value.trim().toLowerCase();
    const st = document.getElementById('status').value;
    const onlyRev = document.getElementById('review').checked;
    const sort = document.getElementById('sort').value;

    let rows = ROWS.filter(r => {
      if (st && r.status !== st) return false;
      if (onlyRev && !r.needs_review) return false;
      if (!q) return true;
      return (r.school + ' ' + r.title + ' ' + r.evidence + ' ' + r.start + ' ' + r.end).toLowerCase().includes(q);
    });

    const cmp = {
      end_asc: (a,b)=> (a.end||'9999') < (b.end||'9999') ? -1 : 1,
      end_desc: (a,b)=> (a.end||'0000') > (b.end||'0000') ? -1 : 1,
      start_asc: (a,b)=> (a.start||'9999') < (b.start||'9999') ? -1 : 1,
      pub_desc: (a,b)=> (a.published||'') > (b.published||'') ? -1 : 1,
      school: (a,b)=> a.school.localeCompare(b.school, 'zh-Hant')
    }[sort];
    rows.sort(cmp);

    document.getElementById('count').textContent = `顯示 ${rows.length} / ${ROWS.length} 筆`;

    tb.innerHTML = rows.map((r, i) => {
      const atts = (r.attachments||[]).map(a =>
        `<a href="${esc(a.url)}" target="_blank" rel="noopener" title="${esc(a.filename)}">${esc((a.filename||'附件').slice(0,18))}</a>`
      ).join('') || '<span style="color:#98a2b3">—</span>';
      const rev = r.needs_review ? '<span class="review">需人工複核</span>' : '';
      const conf = r.confidence ? `信心 ${r.confidence}` : '';
      return `<tr data-i="${i}">
        <td><span class="cell-label">學校</span><span class="school">${esc(r.school)}</span></td>
        <td class="title">
          <span class="cell-label">公告標題</span>
          <a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title)}</a>${rev}
          <button class="frag-toggle" data-i="${i}">▸ 原文片段 ${conf}</button>
          <div class="evidence">${esc(r.evidence || '（未在標題／內文／附件中找到可辨識的日期區間）')}</div>
        </td>
        <td class="d"><span class="cell-label">公告日期</span>${esc(r.published)}</td>
        <td class="d"><span class="cell-label">申請開始</span>${r.start ? esc(r.start)+'<br><span style="color:#98a2b3;font-size:11.5px">'+esc(r.start_roc)+'</span>' : '<span style="color:#98a2b3">—</span>'}</td>
        <td class="d"><span class="cell-label">申請結束</span>${r.end ? esc(r.end)+'<br><span style="color:#98a2b3;font-size:11.5px">'+esc(r.end_roc)+'</span>' : '<span style="color:#98a2b3">—</span>'}</td>
        <td><span class="cell-label">狀態</span><span class="badge ${STATUS_CLASS[r.status]||'b-dead'}">${esc(r.status)}</span></td>
        <td class="att"><span class="cell-label">附件</span>${atts}</td>
      </tr>`;
    }).join('');

    document.getElementById('empty').style.display = rows.length ? 'none' : 'block';
  }

  tb.addEventListener('click', e => {
    const btn = e.target.closest('.frag-toggle');
    if (!btn) return;
    const tr = btn.closest('tr');
    tr.classList.toggle('open');
    btn.textContent = (tr.classList.contains('open') ? '▾ 收起原文片段 ' : '▸ 原文片段 ') + (btn.dataset.conf || '');
  });

  ['q','status','sort','review'].forEach(id => {
    document.getElementById(id).addEventListener('input', render);
    document.getElementById(id).addEventListener('change', render);
  });

  cards(); sourceTags(); render();
})();
</script>
</body>
</html>
"""


def render(path: str, rows: list[dict], sources: list[dict], run: dict | None, total: int, today: date) -> None:
    payload = json.dumps(
        {"rows": rows, "sources": sources, "run": run},
        ensure_ascii=False,
        default=str,
    ).replace("</", "<\\/")
    last_run = ""
    if run and run.get("finished_at"):
        last_run = str(run["finished_at"]).replace("T", " ")
    html_text = (
        TEMPLATE.replace("__DATA__", payload)
        .replace("__GENERATED__", datetime.now().strftime("%Y-%m-%d %H:%M"))
        .replace("__TOTAL__", str(total))
        .replace("__LASTRUN__", html.escape(last_run or "首次執行"))
        .replace("__TODAY__", today.isoformat())
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_text)
