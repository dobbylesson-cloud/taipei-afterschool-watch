#!/usr/bin/env python3
"""學校清單管理介面（本機專用）。

用法：
  python manage.py                # 啟動後自動開啟 http://127.0.0.1:8765/
  python manage.py --no-browser   # 不自動開瀏覽器（適合背景執行／測試）
  python manage.py --port 8777    # 換 port

特色：
  * 只綁定 127.0.0.1，不對外開放。
  * 直接編輯 sources/schools.yaml，使用 ruamel.yaml round-trip 盡量保留註解與原有欄位。
  * 每次寫入前備份為 schools.yaml.bak.<時間戳>，採「暫存檔 + os.replace」原子寫入。
  * 「測試」按鈕只抓公告列表（不寫 DB、不下載附件）。
本檔不會被 run_daily.py 匯入，兩者互不影響。
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from flask import Flask, jsonify, request
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from watcher.adapters.ccms import _with_page
from watcher.adapters.nss import scan_json
from watcher.config import Config
from watcher.http import Fetcher

ROOT = os.path.dirname(os.path.abspath(__file__))
YAML_PATH = os.path.join(ROOT, "sources", "schools.yaml")
DB_PATH = os.path.join(ROOT, "data", "watch.db")

HOST = "127.0.0.1"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
TYPES = ("nss", "ccms")
URL_FIELD = {"nss": "base_url", "ccms": "list_url"}

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096
_yaml.indent(mapping=2, sequence=4, offset=2)

_LOCK = threading.Lock()
log = logging.getLogger("manage")


# ---------------------------------------------------------------- YAML 存取
def _load_doc():
    with open(YAML_PATH, encoding="utf-8") as f:
        doc = _yaml.load(f)
    if doc is None:
        raise RuntimeError("schools.yaml 是空的")
    if "sources" not in doc or doc["sources"] is None:
        doc["sources"] = []
    return doc


def _plain_source(item) -> dict:
    def _lst(v):
        return [str(x) for x in v] if isinstance(v, list) else []

    return {
        "id": str(item.get("id") or ""),
        "name": str(item.get("name") or item.get("id") or ""),
        "type": str(item.get("type") or "nss"),
        "base_url": str(item.get("base_url") or ""),
        "list_url": str(item.get("list_url") or ""),
        "page_keys": _lst(item.get("page_keys")),
        "keywords": _lst(item.get("keywords")),
        "selector": str(item.get("selector") or ""),
        "note": str(item.get("note") or ""),
        "disabled": bool(item.get("disabled", False)),
        "max_items": item.get("max_items"),
        "max_pages": item.get("max_pages"),
        "url": str(item.get("base_url") or item.get("list_url") or ""),
    }


def _save_doc(doc) -> str:
    """備份 → 原子寫入 → 以 Config 驗證可載入；失敗自動回滾。"""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = f"{YAML_PATH}.bak.{ts}"
    n = 2
    while os.path.exists(bak):  # 同一秒內多次寫入時避免覆蓋前一份備份
        bak = f"{YAML_PATH}.bak.{ts}-{n}"
        n += 1
    shutil.copy2(YAML_PATH, bak)

    tmp = f"{YAML_PATH}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        _yaml.dump(doc, f)
    os.replace(tmp, YAML_PATH)

    try:
        Config(YAML_PATH)  # 用正式載入器驗證，避免寫出 run_daily.py 讀不动的檔案
    except Exception:
        shutil.copy2(bak, YAML_PATH)
        raise
    return bak


# ---------------------------------------------------------------- 驗證
def _norm(payload) -> dict:
    def txt(key) -> str:
        v = payload.get(key)
        return v.strip() if isinstance(v, str) else ""

    def lst(key) -> list[str]:
        v = payload.get(key)
        parts = v if isinstance(v, list) else re.split(r"[,，\n]", v or "")
        return [str(x).strip() for x in parts if str(x).strip()]

    return {
        "id": txt("id").lower(),
        "name": txt("name"),
        "type": txt("type") or "nss",
        "base_url": txt("base_url").rstrip("/"),
        "list_url": txt("list_url"),
        "page_keys": lst("page_keys"),
        "keywords": lst("keywords"),
        "selector": txt("selector"),
        "note": txt("note"),
        "disabled": bool(payload.get("disabled")),
    }


def _url_ok(u: str) -> bool:
    try:
        p = urlsplit(u)
    except Exception:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)


def _validate(d: dict, existing: set[str], original_id: str | None = None) -> dict:
    errs: dict[str, str] = {}
    if not d["id"]:
        errs["id"] = "id 必填"
    elif not ID_RE.match(d["id"]):
        errs["id"] = "id 只能用 1–40 字元的小寫英數字與連字號（-），且需以英數開頭"
    elif d["id"] in existing and d["id"] != original_id:
        errs["id"] = f"id「{d['id']}」已存在"

    if not d["name"]:
        errs["name"] = "名稱必填"
    elif len(d["name"]) > 80:
        errs["name"] = "名稱請勿超過 80 字"

    if d["type"] not in TYPES:
        errs["type"] = "平台類型只能是 nss 或 ccms"

    field = URL_FIELD.get(d["type"])
    if field:
        if not d[field]:
            errs[field] = f"{'校園網站首頁網址' if field == 'base_url' else '公告列表頁網址'}必填"
        elif not _url_ok(d[field]):
            errs[field] = "請輸入完整的 http:// 或 https:// 網址"

    if d["selector"] and len(d["selector"]) > 120:
        errs["selector"] = "selector 請勿超過 120 字"

    return errs


def _apply(entry, d: dict) -> None:
    entry["id"] = d["id"]
    entry["name"] = d["name"]
    entry["type"] = d["type"]

    field = URL_FIELD.get(d["type"])
    other = "list_url" if field == "base_url" else "base_url"
    if field:
        entry[field] = d[field]
    if other in entry:
        del entry[other]

    for key in ("page_keys", "keywords"):
        if d[key]:
            entry[key] = list(d[key])
        elif key in entry:
            del entry[key]

    for key in ("selector", "note"):
        if d[key]:
            entry[key] = d[key]
        elif key in entry:
            del entry[key]

    if d["disabled"]:
        entry["disabled"] = True
    elif "disabled" in entry:
        del entry["disabled"]


# ---------------------------------------------------------------- 資料庫統計（唯讀）
def _db_stats() -> dict:
    out = {"exists": os.path.exists(DB_PATH), "notices": 0, "attachments": 0,
           "last_run": None, "per_source": {}}
    if not out["exists"]:
        return out
    try:
        uri = Path(DB_PATH).as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True)
    except Exception:
        return out
    con.row_factory = sqlite3.Row
    try:
        out["notices"] = con.execute("SELECT COUNT(*) c FROM notices").fetchone()["c"]
        out["attachments"] = con.execute(
            "SELECT COUNT(*) c FROM attachments WHERE ok=1").fetchone()["c"]
        for r in con.execute("SELECT source_id, COUNT(*) c FROM notices GROUP BY source_id"):
            out["per_source"][r["source_id"]] = r["c"]
        r = con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        if r:
            out["last_run"] = {
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
                "total": r["total_count"],
                "new": r["new_count"],
                "updated": r["updated_count"],
                "ok_sources": r["ok_sources"],
                "failed_sources": r["failed_sources"],
            }
    except Exception as exc:
        log.warning("讀取資料庫統計失敗：%s", exc)
    finally:
        con.close()
    return out


# ---------------------------------------------------------------- 來源連線測試
def _fetch_with_status(fetcher: Fetcher, url: str):
    """回傳 (status_code, html, err)；err 為人類可讀的失敗原因。"""
    if not fetcher.allowed(url):
        return None, None, f"robots.txt 阻擋，不允許抓取 {url}"
    resp = fetcher.get(url)
    if resp is None:
        return None, None, f"連線失敗：{_probe_error(fetcher, url)}"
    if resp.status_code != 200:
        return resp.status_code, None, f"HTTP {resp.status_code}（{url}）"
    return resp.status_code, resp.text, None


def _probe_error(fetcher: Fetcher, url: str) -> str:
    """重試耗盡時再打一次，取得實際的例外訊息（僅失敗時多做 1 次請求）。"""
    try:
        fetcher.session.get(url, timeout=fetcher.timeout, verify=False)
        return "請求未取得回應（已重試）"
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        return msg[:180]


def _test_nss(cfg: Config, src, fetcher: Fetcher) -> dict:
    out = {"http_status": None, "api_status": None, "modules": 0,
           "items": 0, "matched": 0, "error": ""}
    base = (src.base_url or "").rstrip("/")
    if not base:
        out["error"] = "未設定 base_url"
        return out

    code, html, err = _fetch_with_status(fetcher, f"{base}/nss/p/index")
    out["http_status"] = code
    if err:
        out["error"] = err
        return out

    deploy = scan_json(html, "deployMap") or []
    page_keys = cfg.page_keys_for(src)
    scored = []
    for d in deploy:
        if not isinstance(d, dict) or not d.get("mid") or not d.get("sid"):
            continue
        name = (d.get("setting") or {}).get("name") or ""
        score = 0
        for i, k in enumerate(page_keys):
            if k in name:
                score = 100 - i
                break
        scored.append((score, name, d["mid"], d["sid"]))
    scored.sort(key=lambda x: (-x[0], len(x[1])))

    if not scored:
        out["error"] = "看板找不到：首頁 deployMap 沒有可用的公告模組"
        return out

    # 與 run_daily 相同：依序看前 5 個看板，很多學校同時掛著空看板，看到有資料就停
    picks = [x for x in scored if x[0] > 0][:5] or scored[:1]
    out["modules"] = len(picks)
    keywords = cfg.keywords_for(src)
    excludes = cfg.exclude_keywords
    last_code = None

    for _score, name, mid, sid in picks:
        url = f"{base}/nss/site/main/storage/{mid}/{sid}/find/"
        resp = fetcher.request(
            "POST", url,
            json={"option": {"page": 1, "number": 20, "sort": {"stime": -1}}},
            headers={"Referer": f"{base}/nss/p/index"},
        )
        if resp is None:
            out["error"] = out["error"] or f"看板「{name}」公告 API 無回應（連線失敗或被阻擋）"
            continue
        last_code = resp.status_code
        if resp.status_code != 200:
            out["error"] = out["error"] or f"看板「{name}」公告 API 回傳 HTTP {resp.status_code}"
            continue
        try:
            data = resp.json()
        except Exception:
            out["error"] = out["error"] or f"看板「{name}」公告 API 回應非 JSON"
            continue
        result = ((data.get("data") or {}).get("result")) or []
        out["items"] += len(result)
        for raw in result:
            item = raw.get("data") or {}
            title = re.sub(r"\s+", " ", str(item.get("name") or "")).strip()
            if not title or any(x in title for x in excludes):
                continue
            text = BeautifulSoup(str(item.get("content") or ""), "lxml").get_text("\n", strip=True)
            if any(k in f"{title}\n{text}" for k in keywords):
                out["matched"] += 1
        if out["items"]:  # 找到有資料的看板就不用再試下一個
            break

    out["api_status"] = last_code
    return out


def _test_ccms(cfg: Config, src, fetcher: Fetcher) -> dict:
    out = {"http_status": None, "api_status": None, "modules": 0,
           "items": 0, "matched": 0, "error": ""}
    if not src.list_url:
        out["error"] = "未設定 list_url"
        return out

    url = _with_page(src.list_url, 1)
    code, html, err = _fetch_with_status(fetcher, url)
    out["http_status"] = code
    if err:
        out["error"] = err
        return out

    keywords = cfg.keywords_for(src)
    excludes = cfg.exclude_keywords
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    for tr in soup.find_all("tr"):
        a = tr.find("a", href=lambda h: h and "News_Content.aspx" in h and "s=" in h)
        if not a:
            continue
        href = urljoin(url, a["href"])
        if href in seen:
            continue
        title = re.sub(r"\s+", " ", (a.get("title") or a.get_text(" ", strip=True) or "")).strip()
        if not title or title == "申辦資訊":
            continue
        seen.add(href)
        out["items"] += 1
        if any(k in title for k in keywords) and not any(x in title for x in excludes):
            out["matched"] += 1

    if out["items"] == 0:
        out["error"] = "列表頁沒有解析到任何公告（版面可能改版、被阻擋或不是 CCMS）"
    return out


# ---------------------------------------------------------------- Flask
app = Flask(__name__)


@app.get("/")
def index():
    return PAGE


@app.get("/api/sources")
def api_sources():
    with _LOCK:
        doc = _load_doc()
        sources = [_plain_source(s) for s in (doc.get("sources") or [])]
    db = _db_stats()
    for s in sources:
        s["db_notices"] = db["per_source"].get(s["id"], 0)
    return jsonify({
        "sources": sources,
        "stats": {
            "total": len(sources),
            "active": sum(1 for s in sources if not s["disabled"]),
            "disabled": sum(1 for s in sources if s["disabled"]),
            "notices": db["notices"],
            "attachments": db["attachments"],
            "last_run": db["last_run"],
            "db_exists": db["exists"],
            "yaml_path": YAML_PATH,
            "db_path": DB_PATH,
        },
    })


@app.post("/api/sources")
def api_create():
    d = _norm(request.get_json(silent=True) or {})
    with _LOCK:
        doc = _load_doc()
        existing = {str(s.get("id")) for s in (doc.get("sources") or [])}
        errs = _validate(d, existing)
        if errs:
            return jsonify({"errors": errs}), 400
        entry = CommentedMap()
        _apply(entry, d)
        doc["sources"].append(entry)
        bak = _save_doc(doc)
    log.info("新增來源 %s（備份 %s）", d["id"], os.path.basename(bak))
    return jsonify({"ok": True, "source": _plain_source(entry),
                    "backup": os.path.basename(bak)}), 201


@app.put("/api/sources/<sid>")
def api_update(sid: str):
    d = _norm(request.get_json(silent=True) or {})
    with _LOCK:
        doc = _load_doc()
        items = doc.get("sources") or []
        existing = {str(s.get("id")) for s in items}
        idx = next((i for i, s in enumerate(items) if str(s.get("id")) == sid), None)
        if idx is None:
            return jsonify({"message": f"找不到來源 {sid}"}), 404
        errs = _validate(d, existing, original_id=sid)
        if errs:
            return jsonify({"errors": errs}), 400
        entry = items[idx]
        _apply(entry, d)
        bak = _save_doc(doc)
    log.info("更新來源 %s → %s（備份 %s）", sid, d["id"], os.path.basename(bak))
    return jsonify({"ok": True, "source": _plain_source(entry),
                    "backup": os.path.basename(bak)})


@app.post("/api/sources/<sid>/toggle")
def api_toggle(sid: str):
    payload = request.get_json(silent=True) or {}
    with _LOCK:
        doc = _load_doc()
        item = next((s for s in (doc.get("sources") or []) if str(s.get("id")) == sid), None)
        if item is None:
            return jsonify({"message": f"找不到來源 {sid}"}), 404
        disabled = payload.get("disabled")
        if disabled is None:
            disabled = not bool(item.get("disabled", False))
        if disabled:
            item["disabled"] = True
        elif "disabled" in item:
            del item["disabled"]
        bak = _save_doc(doc)
    state = "停用" if disabled else "啟用"
    log.info("%s 來源 %s（備份 %s）", state, sid, os.path.basename(bak))
    return jsonify({"ok": True, "disabled": bool(disabled),
                    "backup": os.path.basename(bak)})


@app.delete("/api/sources/<sid>")
def api_delete(sid: str):
    with _LOCK:
        doc = _load_doc()
        items = doc.get("sources") or []
        idx = next((i for i, s in enumerate(items) if str(s.get("id")) == sid), None)
        if idx is None:
            return jsonify({"message": f"找不到來源 {sid}"}), 404
        name = str(items[idx].get("name") or sid)
        items.pop(idx)
        bak = _save_doc(doc)
    log.info("刪除來源 %s（%s）（備份 %s）", sid, name, os.path.basename(bak))
    return jsonify({"ok": True, "backup": os.path.basename(bak)})


@app.post("/api/sources/<sid>/test")
def api_test(sid: str):
    with _LOCK:
        cfg = Config(YAML_PATH)
        src = next((s for s in cfg.sources if s.id == sid), None)
    if src is None:
        return jsonify({"message": f"找不到來源 {sid}"}), 404

    # 測試模式：縮短逾時與重試，避免介面卡住；仍維持 1 秒左右的禮貌間隔
    cfg.settings = dict(cfg.settings)
    cfg.settings.update({"timeout": 8, "retries": 1, "interval_min": 0.8, "interval_max": 1.2})

    fetcher = Fetcher(cfg)
    started = time.monotonic()
    try:
        if src.type == "nss":
            result = _test_nss(cfg, src, fetcher)
        elif src.type == "ccms":
            result = _test_ccms(cfg, src, fetcher)
        else:
            result = {"http_status": None, "api_status": None, "modules": 0,
                      "items": 0, "matched": 0, "error": f"未知平台類型 {src.type}"}
    except Exception as exc:  # 單一測試失敗不影響服務
        log.exception("測試來源 %s 失敗", sid)
        result = {"http_status": None, "api_status": None, "modules": 0,
                  "items": 0, "matched": 0, "error": f"{type(exc).__name__}: {exc}"}

    result.update({
        "id": sid,
        "name": src.name,
        "type": src.type,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "requests": fetcher.stats["requests"],
        "robots_blocked": fetcher.stats["blocked_by_robots"],
        "tested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ok": not result.get("error"),
    })
    return jsonify(result)


# ---------------------------------------------------------------- 頁面
PAGE = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>學校清單管理｜臺北市國小課後社團公告監測</title>
<style>
  :root{
    --bg:#f2f5f9; --panel:#ffffff; --ink:#10233a; --muted:#65758b; --line:#dde5ee;
    --brand:#0f5f8f; --brand2:#0b7f6b; --ok:#0f7a4a; --ok-bg:#e7f6ee;
    --off:#5b6b7c; --off-bg:#eceff3; --warn:#a35c00; --warn-bg:#fff4e2;
    --bad:#a3282b; --bad-bg:#fdecec; --shadow:0 12px 32px rgba(16,40,70,.10);
    --radius:14px;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
    font-family:system-ui,-apple-system,"Segoe UI","Microsoft JhengHei","PingFang TC",sans-serif;
    font-size:14px;line-height:1.6}
  a{color:var(--brand);text-decoration:none}
  a:hover{text-decoration:underline}
  .wrap{max-width:1280px;margin:0 auto;padding:0 20px}

  header.hero{background:linear-gradient(120deg,#0f5f8f 0%,#0b7f6b 100%);color:#fff;padding:26px 0 22px}
  .kicker{font-size:12px;letter-spacing:.18em;opacity:.85}
  h1{font-size:24px;margin:6px 0 4px;letter-spacing:.02em}
  .sub{margin:0;opacity:.9;font-size:13px}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-top:18px}
  .card{background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.22);
    border-radius:12px;padding:10px 14px;backdrop-filter:blur(2px)}
  .card span{display:block;font-size:12px;opacity:.85}
  .card b{font-size:20px;font-weight:650;letter-spacing:.01em}

  main{padding:22px 0 40px}
  .toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;
    background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
    padding:12px;box-shadow:var(--shadow)}
  .toolbar input[type=search]{flex:1 1 260px;min-width:200px}
  input,select,textarea{font:inherit;color:inherit;background:#fff;
    border:1px solid var(--line);border-radius:9px;padding:8px 10px;outline:none}
  input:focus,select:focus,textarea:focus{border-color:var(--brand);box-shadow:0 0 0 3px rgba(15,95,143,.12)}
  button{font:inherit;cursor:pointer;border-radius:9px;padding:8px 13px;border:1px solid var(--line);
    background:#fff;color:var(--ink);transition:.15s}
  button:hover{background:#f6f9fc}
  button:disabled{opacity:.55;cursor:not-allowed}
  button.primary{background:var(--brand);border-color:var(--brand);color:#fff;font-weight:600}
  button.primary:hover{background:#0c4e76}
  button.danger{color:var(--bad);border-color:#e9c6c7}
  button.danger:hover{background:var(--bad-bg)}
  button.sm{padding:5px 9px;font-size:12px}
  .muted{color:var(--muted)}

  .tablewrap{margin-top:14px;background:var(--panel);border:1px solid var(--line);
    border-radius:var(--radius);box-shadow:var(--shadow);overflow:auto}
  table{width:100%;border-collapse:collapse;font-size:13.5px}
  thead th{position:sticky;top:0;background:#f7fafc;text-align:left;font-weight:600;
    color:#33475d;padding:11px 12px;border-bottom:1px solid var(--line);white-space:nowrap;z-index:2}
  thead th.sortable{cursor:pointer;user-select:none}
  thead th.sortable:hover{color:var(--brand)}
  thead th .arrow{font-size:11px;color:var(--muted);margin-left:4px}
  tbody td{padding:11px 12px;border-bottom:1px solid #eef3f8;vertical-align:top}
  tbody tr:hover{background:#fafcfe}
  tbody tr.disabled td{color:#8a97a6}
  .sid{font-family:ui-monospace,Consolas,monospace;font-size:12.5px;color:#0d4b74}
  .school{font-weight:600}
  .pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap}
  .pill.on{background:var(--ok-bg);color:var(--ok)}
  .pill.off{background:var(--off-bg);color:var(--off)}
  .pill.type{background:#e8f1f9;color:#0d4b74}
  .url{word-break:break-all;color:#455a70;font-size:12.5px}
  .note{color:var(--muted);font-size:12.5px;max-width:260px}
  .acts{white-space:nowrap;display:flex;gap:6px;flex-wrap:wrap}
  .empty{padding:40px 12px;text-align:center;color:var(--muted)}

  tr.testrow td{background:#f7fbfd;border-bottom:1px solid var(--line);padding:0}
  .testbox{padding:12px 16px;font-size:12.5px}
  .chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px}
  .chip{background:#fff;border:1px solid var(--line);border-radius:8px;padding:3px 9px}
  .chip b{font-weight:650}
  .chip.ok{border-color:#b9e2cd;background:#f2fbf6}
  .chip.bad{border-color:#efc6c7;background:#fdf3f3;color:var(--bad)}
  .testmsg{color:var(--bad)}
  .spinner{display:inline-block;width:12px;height:12px;border:2px solid #cbd6e2;
    border-top-color:var(--brand);border-radius:50%;animation:spin .8s linear infinite;vertical-align:-2px;margin-right:6px}
  @keyframes spin{to{transform:rotate(360deg)}}

  footer{padding:18px 0 40px;color:var(--muted);font-size:12.5px}
  footer .row{display:flex;flex-wrap:wrap;gap:16px}

  .overlay{position:fixed;inset:0;background:rgba(12,26,44,.45);display:none;
    align-items:flex-start;justify-content:center;padding:40px 16px;overflow:auto;z-index:20}
  .overlay.show{display:flex}
  .modal{width:100%;max-width:760px;background:var(--panel);border-radius:16px;
    box-shadow:0 24px 60px rgba(10,25,45,.28);padding:22px 24px}
  .modal h2{margin:0 0 4px;font-size:19px}
  .modal .desc{margin:0 0 16px;color:var(--muted);font-size:12.5px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:12px 16px}
  .grid .full{grid-column:1 / -1}
  label{display:block;font-size:12.5px;font-weight:600;color:#33475d}
  label .req{color:var(--bad);margin-left:2px}
  label input,label select,label textarea{width:100%;margin-top:5px;font-weight:400}
  label textarea{resize:vertical;min-height:64px}
  .hint{display:block;font-weight:400;color:var(--muted);font-size:11.5px;margin-top:4px}
  .fielderr{display:none;color:var(--bad);font-size:11.5px;margin-top:4px}
  .fielderr.show{display:block}
  .formfoot{display:flex;justify-content:flex-end;gap:10px;margin-top:20px}
  .inline{display:flex;align-items:center;gap:8px;font-weight:400}
  .inline input{width:auto;margin:0}
  .hidden{display:none !important}

  #toast{position:fixed;right:20px;bottom:20px;display:flex;flex-direction:column;gap:8px;z-index:40}
  .toast{background:#123;color:#fff;padding:11px 15px;border-radius:10px;font-size:13px;
    box-shadow:var(--shadow);opacity:0;transform:translateY(8px);transition:.25s}
  .toast.show{opacity:1;transform:none}
  .toast.err{background:#7f1d1f}
  .confirm-name{font-weight:700}
  .warnbox{background:var(--warn-bg);color:var(--warn);border-radius:9px;padding:10px 12px;
    font-size:12.5px;margin-top:12px}
  @media (max-width:720px){
    .grid{grid-template-columns:1fr}
    h1{font-size:20px}
  }
</style>
</head>
<body>
<header class="hero">
  <div class="wrap">
    <div class="kicker">TAIPEI AFTER-SCHOOL CLUB WATCHER</div>
    <h1>學校來源清單管理</h1>
    <p class="sub">在本機瀏覽器維護 <b>sources/schools.yaml</b>。每次儲存前會自動備份，寫入採原子替換；管理頁只改設定，不會執行爬蟲。</p>
    <div class="cards">
      <div class="card"><span>啟用來源</span><b id="stat-active">–</b></div>
      <div class="card"><span>停用來源</span><b id="stat-disabled">–</b></div>
      <div class="card"><span>資料庫公告</span><b id="stat-notices">–</b></div>
      <div class="card"><span>最後一次執行</span><b id="stat-lastrun" style="font-size:15px">–</b></div>
    </div>
  </div>
</header>

<main class="wrap">
  <div class="toolbar">
    <input id="q" type="search" placeholder="搜尋 id、名稱、網址、備註…" autocomplete="off">
    <select id="filter" title="狀態篩選">
      <option value="all">全部狀態</option>
      <option value="on">僅啟用</option>
      <option value="off">僅停用</option>
    </select>
    <select id="sort" title="排序">
      <option value="id">依 ID 排序</option>
      <option value="name">依名稱排序</option>
      <option value="type">依平台排序</option>
      <option value="status">依狀態排序</option>
      <option value="url">依網址排序</option>
    </select>
    <button id="btn-refresh">重新載入</button>
    <button id="btn-new" class="primary">＋ 新增來源</button>
    <span id="count" class="muted"></span>
  </div>

  <div class="tablewrap">
    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>學校／單位名稱</th>
          <th>平台</th>
          <th>網址</th>
          <th>狀態</th>
          <th>資料庫筆數</th>
          <th>備註</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody id="rows"></tbody>
    </table>
  </div>

  <footer>
    <div class="row">
      <span>設定檔：<span id="path-yaml" class="muted">–</span></span>
      <span>資料庫：<span id="path-db" class="muted">–</span></span>
    </div>
    <div class="row" style="margin-top:6px">
      <span>此頁僅監聽 127.0.0.1，關閉視窗後服務仍會執行，請在終端機按 Ctrl+C 結束。</span>
    </div>
  </footer>
</main>

<!-- 新增／編輯 -->
<div class="overlay" id="form-overlay">
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="form-title">
    <h2 id="form-title">新增來源</h2>
    <p class="desc" id="form-desc">儲存後會立即寫回 schools.yaml，並在寫入前建立備份。</p>
    <div class="grid">
      <label>ID<span class="req">*</span>
        <input name="id" placeholder="例如 xyzps" autocomplete="off">
        <span class="hint">唯一識別碼：小寫英數與連字號，建立後可修改但建議固定。</span>
        <span class="fielderr" data-err="id"></span>
      </label>
      <label>名稱<span class="req">*</span>
        <input name="name" placeholder="例如 臺北市○○區○○國民小學" autocomplete="off">
        <span class="fielderr" data-err="name"></span>
      </label>
      <label>平台類型<span class="req">*</span>
        <select name="type">
          <option value="nss">nss（臺北市校園網站系統）</option>
          <option value="ccms">ccms（臺北市政府 CCMS）</option>
        </select>
        <span class="fielderr" data-err="type"></span>
      </label>
      <label data-field="base_url">校園網站首頁<span class="req">*</span>
        <input name="base_url" placeholder="https://www.xyzps.tp.edu.tw" autocomplete="off">
        <span class="hint">nss 只需首頁網址，程式會自動找出公告看板。</span>
        <span class="fielderr" data-err="base_url"></span>
      </label>
      <label data-field="list_url" class="hidden">公告列表頁<span class="req">*</span>
        <input name="list_url" placeholder="https://www.doe.gov.taipei/News.aspx?n=...&amp;sms=..." autocomplete="off">
        <span class="hint">ccms 需提供 News.aspx 列表頁完整網址。</span>
        <span class="fielderr" data-err="list_url"></span>
      </label>
      <label>關鍵字覆寫（選填）
        <input name="keywords" placeholder="以逗號分隔，例如 社團, 課後照顧" autocomplete="off">
        <span class="hint">留空表示沿用全域關鍵字。</span>
        <span class="fielderr" data-err="keywords"></span>
      </label>
      <label>看板關鍵字覆寫（選填，nss）
        <input name="page_keys" placeholder="例如 最新消息, 校園公告" autocomplete="off">
        <span class="hint">留空表示沿用全域 page_keys。</span>
      </label>
      <label class="full">CSS selector 覆寫（選填）
        <input name="selector" placeholder="例如 #CCMS_Content 或 .news-list" autocomplete="off">
        <span class="hint">僅保存於設定檔；目前 nss／ccms 轉接器尚未讀取此欄位，保留給自訂來源或後續擴充。</span>
        <span class="fielderr" data-err="selector"></span>
      </label>
      <label class="full">備註（選填）
        <textarea name="note" placeholder="例如：2026-11 實測站台連線失敗，待確認新網址"></textarea>
        <span class="hint">會顯示在此列表與 index.html 的來源狀態區。</span>
      </label>
      <label class="full inline">
        <input type="checkbox" name="disabled"> 停用此來源（仍保留設定，不參與每日掃描）
      </label>
    </div>
    <div class="formfoot">
      <button id="form-cancel">取消</button>
      <button id="form-save" class="primary">儲存</button>
    </div>
  </div>
</div>

<!-- 刪除確認 -->
<div class="overlay" id="del-overlay">
  <div class="modal" style="max-width:520px" role="dialog" aria-modal="true">
    <h2>確認刪除來源？</h2>
    <p class="desc">將從 schools.yaml 移除 <span class="confirm-name" id="del-name">–</span>（<span id="del-id">–</span>）。此動作會先建立備份，但無法從介面復原。</p>
    <div id="del-warn" class="warnbox hidden"></div>
    <div class="formfoot">
      <button id="del-cancel">取消</button>
      <button id="del-ok" class="danger">確認刪除</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
const state = { rows: [], stats: null, q: '', filter: 'all', sort: 'id', editing: null, deleting: null };

const $ = (s, root=document) => root.querySelector(s);
const $$ = (s, root=document) => Array.from(root.querySelectorAll(s));

function esc(v){
  return String(v === null || v === undefined ? '' : v).replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
}

function toast(msg, isErr){
  const box = $('#toast');
  const el = document.createElement('div');
  el.className = 'toast' + (isErr ? ' err' : '');
  el.textContent = msg;
  box.appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));
  setTimeout(() => { el.classList.remove('show'); setTimeout(() => el.remove(), 300); }, 3200);
}

async function api(url, opts){
  const res = await fetch(url, Object.assign({headers:{'Content-Type':'application/json'}}, opts || {}));
  let data = {};
  try { data = await res.json(); } catch(e) {}
  if(!res.ok){
    const err = new Error(data.message || '請求失敗（HTTP ' + res.status + '）');
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

function fmtTime(iso){
  if(!iso) return '–';
  const d = new Date(iso);
  if(isNaN(d)) return iso;
  const p = n => String(n).padStart(2,'0');
  return d.getFullYear() + '-' + p(d.getMonth()+1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

function statusKey(s){ return s.disabled ? 'off' : 'on'; }

function displayUrl(s){
  const u = s.type === 'ccms' ? s.list_url : s.base_url;
  return u || '（未設定）';
}

function visibleRows(){
  const q = state.q.trim().toLowerCase();
  let rows = state.rows.filter(s => {
    if(state.filter === 'on' && s.disabled) return false;
    if(state.filter === 'off' && !s.disabled) return false;
    if(!q) return true;
    const blob = [s.id, s.name, s.type, s.base_url, s.list_url, s.note, (s.keywords||[]).join(' '), (s.page_keys||[]).join(' ')].join(' ').toLowerCase();
    return blob.indexOf(q) >= 0;
  });
  const key = state.sort;
  rows.sort((a,b) => {
    let va, vb;
    if(key === 'status'){ va = statusKey(a); vb = statusKey(b); }
    else if(key === 'url'){ va = displayUrl(a); vb = displayUrl(b); }
    else if(key === 'name'){ va = a.name; vb = b.name; }
    else if(key === 'type'){ va = a.type; vb = b.type; }
    else { va = a.id; vb = b.id; }
    va = String(va).toLowerCase(); vb = String(vb).toLowerCase();
    return va < vb ? -1 : va > vb ? 1 : 0;
  });
  return rows;
}

function renderStats(){
  const st = state.stats || {};
  $('#stat-active').textContent = st.active ?? '–';
  $('#stat-disabled').textContent = st.disabled ?? '–';
  $('#stat-notices').textContent = st.notices ?? '–';
  const lr = st.last_run;
  $('#stat-lastrun').textContent = lr ? fmtTime(lr.finished_at || lr.started_at) : '尚未執行';
  $('#path-yaml').textContent = st.yaml_path || '–';
  $('#path-db').textContent = st.db_path || '–';
}

function renderTable(){
  const rows = visibleRows();
  const tbody = $('#rows');
  $('#count').textContent = '顯示 ' + rows.length + ' / ' + state.rows.length + ' 筆';
  if(!rows.length){
    tbody.innerHTML = '<tr><td colspan="8" class="empty">沒有符合條件的來源</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(s => {
    const url = displayUrl(s);
    const link = url && url !== '（未設定）'
      ? '<a class="url" href="' + esc(url) + '" target="_blank" rel="noreferrer">' + esc(url) + '</a>'
      : '<span class="muted">（未設定）</span>';
    return '' +
      '<tr data-id="' + esc(s.id) + '" class="' + (s.disabled ? 'disabled' : '') + '">' +
        '<td class="sid">' + esc(s.id) + '</td>' +
        '<td class="school">' + esc(s.name) + '</td>' +
        '<td><span class="pill type">' + esc(s.type) + '</span></td>' +
        '<td>' + link + '</td>' +
        '<td><span class="pill ' + (s.disabled ? 'off' : 'on') + '">' + (s.disabled ? '停用' : '啟用') + '</span></td>' +
        '<td class="muted">' + (s.db_notices || 0) + '</td>' +
        '<td class="note">' + (s.note ? esc(s.note) : '<span class="muted">—</span>') + '</td>' +
        '<td><div class="acts">' +
          '<button class="sm" data-act="test">測試</button>' +
          '<button class="sm" data-act="edit">編輯</button>' +
          '<button class="sm" data-act="toggle">' + (s.disabled ? '啟用' : '停用') + '</button>' +
          '<button class="sm danger" data-act="delete">刪除</button>' +
        '</div></td>' +
      '</tr>';
  }).join('');
}

async function load(){
  try{
    const d = await api('/api/sources');
    state.rows = d.sources;
    state.stats = d.stats;
    renderStats();
    renderTable();
  }catch(e){
    toast('載入失敗：' + e.message, true);
  }
}

function findSource(id){
  for(const s of state.rows) if(s.id === id) return s;
  return null;
}

/* ---------- 測試 ---------- */
async function runTest(id, btn){
  const row = btn.closest('tr');
  let box = row.nextElementSibling;
  if(!box || !box.classList.contains('testrow')){
    box = document.createElement('tr');
    box.className = 'testrow';
    box.innerHTML = '<td colspan="8"><div class="testbox"></div></td>';
    row.parentNode.insertBefore(box, row.nextSibling);
  }
  const inner = box.querySelector('.testbox');
  inner.innerHTML = '<span class="spinner"></span>連線測試中（不下載附件、不寫入資料庫，最長約 20 秒）…';
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = '測試中…';
  try{
    const r = await api('/api/sources/' + encodeURIComponent(id) + '/test', {method:'POST'});
    const http = r.http_status === null ? '未取得' : r.http_status;
    const apiCode = r.api_status === null || r.api_status === undefined ? '' :
      '<span class="chip"><b>API</b> ' + r.api_status + '</span>';
    const mod = r.type === 'nss' ? '<span class="chip"><b>看板</b> ' + r.modules + ' 個</span>' : '';
    inner.innerHTML = '<div class="chips">' +
        '<span class="chip ' + (r.http_status === 200 ? 'ok' : 'bad') + '"><b>HTTP</b> ' + http + '</span>' +
        apiCode + mod +
        '<span class="chip"><b>列表</b> ' + r.items + ' 筆</span>' +
        '<span class="chip"><b>命中關鍵字</b> ' + r.matched + ' 筆</span>' +
        '<span class="chip"><b>耗時</b> ' + (r.elapsed_ms / 1000).toFixed(1) + ' 秒</span>' +
        '<span class="chip"><b>請求</b> ' + r.requests + ' 次</span>' +
      '</div>' +
      (r.error
        ? '<div class="testmsg">✕ ' + esc(r.error) + '</div>'
        : '<div class="muted">✓ 列表可正常取得（測試時間 ' + esc(r.tested_at) + '；實際掃描仍由 run_daily.py 執行）</div>');
  }catch(e){
    inner.innerHTML = '<div class="testmsg">✕ 測試失敗：' + esc(e.message) + '</div>';
  }finally{
    btn.disabled = false;
    btn.textContent = label;
  }
}

/* ---------- 表單 ---------- */
function openForm(src){
  state.editing = src ? src.id : null;
  const form = $('#form-overlay');
  const modal = $('#form-overlay .modal');
  $('#form-title').textContent = src ? '編輯來源：' + src.name : '新增來源';
  $('#form-desc').textContent = src
    ? '修改後會寫回 schools.yaml（寫入前自動備份）。ID 變更後，舊 ID 的歷史公告仍留在資料庫。'
    : '儲存後會立即寫回 schools.yaml，並在寫入前建立備份。';

  const inputs = modal.querySelectorAll('input, select, textarea');
  inputs.forEach(el => { if(el.type === 'checkbox') el.checked = false; else el.value = ''; });
  if(src){
    modal.querySelector('[name=id]').value = src.id;
    modal.querySelector('[name=name]').value = src.name;
    modal.querySelector('[name=type]').value = src.type;
    modal.querySelector('[name=base_url]').value = src.base_url || '';
    modal.querySelector('[name=list_url]').value = src.list_url || '';
    modal.querySelector('[name=keywords]').value = (src.keywords || []).join(', ');
    modal.querySelector('[name=page_keys]').value = (src.page_keys || []).join(', ');
    modal.querySelector('[name=selector]').value = src.selector || '';
    modal.querySelector('[name=note]').value = src.note || '';
    modal.querySelector('[name=disabled]').checked = !!src.disabled;
  }
  clearErrors();
  syncType();
  form.classList.add('show');
  setTimeout(() => modal.querySelector('[name=id]').focus(), 60);
}

function closeForm(){ $('#form-overlay').classList.remove('show'); state.editing = null; }

function syncType(){
  const modal = $('#form-overlay .modal');
  const type = modal.querySelector('[name=type]').value;
  $$('[data-field]', modal).forEach(el => {
    el.classList.toggle('hidden', el.getAttribute('data-field') !== type);
  });
}

function clearErrors(){
  $$('#form-overlay .fielderr').forEach(el => { el.classList.remove('show'); el.textContent = ''; });
}

function showErrors(errs){
  clearErrors();
  Object.keys(errs).forEach(k => {
    const el = $('#form-overlay [data-err="' + k + '"]');
    if(el){ el.textContent = errs[k]; el.classList.add('show'); }
  });
}

function formData(){
  const modal = $('#form-overlay .modal');
  const val = n => (modal.querySelector('[name=' + n + ']') || {}).value || '';
  return {
    id: val('id').trim().toLowerCase(),
    name: val('name').trim(),
    type: val('type'),
    base_url: val('base_url').trim(),
    list_url: val('list_url').trim(),
    keywords: val('keywords').trim(),
    page_keys: val('page_keys').trim(),
    selector: val('selector').trim(),
    note: val('note').trim(),
    disabled: modal.querySelector('[name=disabled]').checked
  };
}

function validateLocal(d, originalId){
  const errs = {};
  if(!d.id) errs.id = 'id 必填';
  else if(!/^[a-z0-9][a-z0-9-]{0,39}$/.test(d.id)) errs.id = 'id 只能用 1–40 字元的小寫英數與連字號（-），且需以英數開頭';
  else if(d.id !== originalId && state.rows.some(s => s.id === d.id)) errs.id = 'id「' + d.id + '」已存在';
  if(!d.name) errs.name = '名稱必填';
  const urlOk = u => {
    try{ const x = new URL(u); return x.protocol === 'http:' || x.protocol === 'https:'; }
    catch(e){ return false; }
  };
  if(d.type === 'nss'){
    if(!d.base_url) errs.base_url = '校園網站首頁網址必填';
    else if(!urlOk(d.base_url)) errs.base_url = '請輸入完整的 http:// 或 https:// 網址';
  }else if(d.type === 'ccms'){
    if(!d.list_url) errs.list_url = '公告列表頁網址必填';
    else if(!urlOk(d.list_url)) errs.list_url = '請輸入完整的 http:// 或 https:// 網址';
  }
  return errs;
}

async function saveForm(){
  const d = formData();
  const errs = validateLocal(d, state.editing);
  if(Object.keys(errs).length){ showErrors(errs); toast('請先修正表單紅字欄位', true); return; }
  const btn = $('#form-save');
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = '儲存中…';
  try{
    let r;
    if(state.editing){
      r = await api('/api/sources/' + encodeURIComponent(state.editing), {method:'PUT', body: JSON.stringify(d)});
    }else{
      r = await api('/api/sources', {method:'POST', body: JSON.stringify(d)});
    }
    closeForm();
    toast('已儲存（備份：' + (r.backup || '—') + '）');
    await load();
  }catch(e){
    if(e.data && e.data.errors){ showErrors(e.data.errors); }
    toast('儲存失敗：' + e.message, true);
  }finally{
    btn.disabled = false;
    btn.textContent = label;
  }
}

/* ---------- 刪除 ---------- */
function openDelete(src){
  state.deleting = src.id;
  $('#del-name').textContent = src.name;
  $('#del-id').textContent = src.id;
  const warn = $('#del-warn');
  if(src.db_notices){
    warn.classList.remove('hidden');
    warn.textContent = '注意：資料庫中仍有 ' + src.db_notices + ' 筆此來源的歷史公告。刪除來源只會移除設定，不會刪除歷史資料。';
  }else{
    warn.classList.add('hidden');
    warn.textContent = '';
  }
  $('#del-overlay').classList.add('show');
}

function closeDelete(){ $('#del-overlay').classList.remove('show'); state.deleting = null; }

async function doDelete(){
  const id = state.deleting;
  if(!id) return;
  const btn = $('#del-ok');
  btn.disabled = true;
  btn.textContent = '刪除中…';
  try{
    const r = await api('/api/sources/' + encodeURIComponent(id), {method:'DELETE'});
    closeDelete();
    toast('已刪除（備份：' + (r.backup || '—') + '）');
    await load();
  }catch(e){
    toast('刪除失敗：' + e.message, true);
  }finally{
    btn.disabled = false;
    btn.textContent = '確認刪除';
  }
}

/* ---------- 事件 ---------- */
$('#rows').addEventListener('click', async ev => {
  const btn = ev.target.closest('button[data-act]');
  if(!btn) return;
  const id = btn.closest('tr').getAttribute('data-id');
  const src = findSource(id);
  if(!src) return;
  const act = btn.getAttribute('data-act');
  if(act === 'test') return runTest(id, btn);
  if(act === 'edit') return openForm(src);
  if(act === 'delete') return openDelete(src);
  if(act === 'toggle'){
    btn.disabled = true;
    try{
      const r = await api('/api/sources/' + encodeURIComponent(id) + '/toggle', {method:'POST', body: '{}'});
      toast((r.disabled ? '已停用 ' : '已啟用 ') + src.name);
      await load();
    }catch(e){
      toast('切換失敗：' + e.message, true);
      btn.disabled = false;
    }
  }
});

$('#q').addEventListener('input', ev => { state.q = ev.target.value; renderTable(); });
$('#filter').addEventListener('change', ev => { state.filter = ev.target.value; renderTable(); });
$('#sort').addEventListener('change', ev => { state.sort = ev.target.value; renderTable(); });
$('#btn-refresh').addEventListener('click', () => load());
$('#btn-new').addEventListener('click', () => openForm(null));
$('#form-cancel').addEventListener('click', closeForm);
$('#form-save').addEventListener('click', saveForm);
$('#form-overlay').addEventListener('click', ev => { if(ev.target.id === 'form-overlay') closeForm(); });
$('#form-overlay .modal [name=type]').addEventListener('change', syncType);
$('#form-overlay .modal [name=id]').addEventListener('input', ev => {
  ev.target.value = ev.target.value.toLowerCase().replace(/[^a-z0-9-]/g, '');
  ev.target.value = ev.target.value.replace(/^-+/, '');
});
$('#del-cancel').addEventListener('click', closeDelete);
$('#del-ok').addEventListener('click', doDelete);
$('#del-overlay').addEventListener('click', ev => { if(ev.target.id === 'del-overlay') closeDelete(); });
document.addEventListener('keydown', ev => {
  if(ev.key === 'Escape'){
    if($('#form-overlay').classList.contains('show')) closeForm();
    if($('#del-overlay').classList.contains('show')) closeDelete();
  }
});

load();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- 啟動
def main() -> int:
    global YAML_PATH, DB_PATH

    ap = argparse.ArgumentParser(description="學校清單管理介面（僅本機）")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--yaml", default=YAML_PATH)
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--no-browser", action="store_true", help="不自動開啟瀏覽器")
    args = ap.parse_args()

    globals()["YAML_PATH"] = os.path.abspath(args.yaml)
    globals()["DB_PATH"] = os.path.abspath(args.db)

    if not os.path.exists(YAML_PATH):
        print(f"找不到設定檔：{YAML_PATH}")
        return 1

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    url = f"http://{HOST}:{args.port}/"
    print("=" * 60)
    print("學校清單管理介面")
    print(f"  設定檔：{YAML_PATH}")
    print(f"  資料庫：{DB_PATH}")
    print(f"  網址  ：{url}（僅限本機）")
    print("  按 Ctrl+C 結束；管理頁只編輯設定，不影響 run_daily.py 的排程")
    print("=" * 60)

    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    try:
        app.run(host=HOST, port=args.port, debug=False, use_reloader=False, threaded=True)
    except OSError as exc:
        print(f"無法啟動（port {args.port} 可能被占用）：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
