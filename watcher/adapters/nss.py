"""NSS（臺北市校園網站系統）來源适配器。

流程：
1. 抓學校首頁 /nss/p/index，從內嵌 JSON 取出 deployMap（每個公告模組的 mid/sid）
   與 pages（頁面代號），第一次抓到的結果會快取 7 天。
2. 對每個「最新消息/公告」模組呼叫 POST /nss/site/{upath}/storage/{mid}/{sid}/find/
   參數 {"option": {"page": n, "number": 20, "sort": {"stime": -1}}}。
3. 依關鍵字過濾標題/內文，再解析內文附件連結。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..attachments import fetch_attachment, find_attachment_links
from ..config import Config, Source
from ..http import Fetcher
from ..models import Notice

log = logging.getLogger("watcher.adapters.nss")


def scan_json(html: str, key: str):
    """在 HTML 中找出 "<key>": 之後的第一個 JSON 物件/陣列並解析。"""
    idx = html.find(f'"{key}":')
    if idx < 0:
        return None
    start = -1
    for i in range(idx + len(key) + 3, min(len(html), idx + len(key) + 400)):
        if html[i] in "[{":
            start = i
            break
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(html)):
        ch = html[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


class NSSAdapter:
    def __init__(self, cfg: Config, fetcher: Fetcher, source: Source, cache_get=None, cache_set=None):
        self.cfg = cfg
        self.fetcher = fetcher
        self.source = source
        self.base = source.base_url.rstrip("/")
        self.cache_get = cache_get
        self.cache_set = cache_set
        self.keywords = cfg.keywords_for(source)
        self.excludes = cfg.exclude_keywords
        self.page_keys = cfg.page_keys_for(source)
        self.max_items = source.max_items or int(cfg.get("max_items", 40))
        self.max_pages = source.max_pages or int(cfg.get("max_pages", 2))
        self.lookback_days = int(cfg.get("lookback_days", 400))
        self.max_attachment_bytes = int(cfg.get("max_attachment_mb", 25)) * 1024 * 1024
        self.max_attachments = int(cfg.get("max_attachments_per_notice", 3))

    # ---------- 取得模組資訊 ----------
    def _load_modules(self) -> list[dict]:
        cached = self.cache_get(self.source.id) if self.cache_get else None
        if cached and cached.get("modules"):
            fetched = datetime.fromisoformat(cached["fetched_at"])
            if datetime.now() - fetched < timedelta(days=7):
                return cached["modules"]

        resp = self.fetcher.get(f"{self.base}/nss/p/index")
        if resp is None or resp.status_code != 200:
            raise RuntimeError("首頁取得失敗")
        html = resp.text
        deploy = scan_json(html, "deployMap") or []
        pages = scan_json(html, "pages") or []
        page_name_by_id = {p.get("_id"): p.get("name") for p in pages if isinstance(p, dict)}
        heading_by_id = {p.get("_id"): p.get("heading") for p in pages if isinstance(p, dict)}

        modules = []
        for d in deploy:
            if not isinstance(d, dict):
                continue
            setting = d.get("setting") or {}
            name = setting.get("name") or ""
            if not d.get("mid") or not d.get("sid"):
                continue
            score = 0
            for i, k in enumerate(self.page_keys):
                if k in name:
                    score = 100 - i
                    break
            if score <= 0:
                continue
            pid = d.get("page")
            modules.append(
                {
                    "name": name,
                    "mid": d["mid"],
                    "sid": d["sid"],
                    "page_name": page_name_by_id.get(pid) or "",
                    "heading": heading_by_id.get(pid) or name,
                    "score": score,
                }
            )
        modules.sort(key=lambda m: (-m["score"], len(m["name"])))
        # 同一個公告模組只保留一次
        seen, uniq = set(), []
        for m in modules:
            if m["sid"] in seen:
                continue
            seen.add(m["sid"])
            uniq.append(m)

        if not uniq:
            # 找不到符合的模組時，退而抓第一個可用的公告模組
            for d in deploy:
                if isinstance(d, dict) and d.get("mid") and d.get("sid"):
                    pid = d.get("page")
                    uniq.append(
                        {
                            "name": (d.get("setting") or {}).get("name", "公告"),
                            "mid": d["mid"],
                            "sid": d["sid"],
                            "page_name": page_name_by_id.get(pid) or "",
                            "heading": heading_by_id.get(pid) or "",
                            "score": 0,
                        }
                    )
                    break

        if self.cache_set and uniq:
            self.cache_set(self.source.id, {"fetched_at": datetime.now().isoformat(), "modules": uniq})
        return uniq

    # ---------- 抓模組內的公告 ----------
    def _fetch_module(self, module: dict) -> list[dict]:
        url = f"{self.base}/nss/site/main/storage/{module['mid']}/{module['sid']}/find/"
        items: list[dict] = []
        for page in range(1, self.max_pages + 1):
            data = self.fetcher.post_json(
                url,
                {"option": {"page": page, "number": 20, "sort": {"stime": -1}}},
                headers={"Referer": f"{self.base}/nss/p/index"},
            )
            if not data:
                break
            result = ((data.get("data") or {}).get("result")) or []
            if not result:
                break
            items.extend(result)
            oldest = min(
                (i.get("data", {}).get("stime") or 0) for i in result
            )
            if oldest and datetime.utcfromtimestamp(oldest / 1000) < datetime.now() - timedelta(
                days=self.lookback_days
            ):
                break
        return items

    def crawl(self) -> tuple[list[Notice], str]:
        modules = self._load_modules()
        if not modules:
            return [], "找不到公告模組（可能網站改版）"

        # 依名稱優先序嘗試前幾個看板（很多學校同時掛著多個空看板，空的會自動略過）
        chosen = modules[:5]
        seen_ids: set[str] = set()
        notices: list[Notice] = []
        for module in chosen:
            raw_items = self._fetch_module(module)
            for raw in raw_items:
                item = raw.get("data") or {}
                item_id = raw.get("_id") or ""
                if not item_id or item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                title = re.sub(r"\s+", " ", (item.get("name") or "")).strip()
                if not title:
                    continue
                html = item.get("content") or ""
                text = BeautifulSoup(html, "lxml").get_text("\n", strip=True)
                if not self._match_keywords(title, text):
                    continue
                published = None
                if item.get("stime"):
                    try:
                        published = datetime.utcfromtimestamp(int(item["stime"]) / 1000).date()
                    except Exception:
                        published = None
                page_name = module.get("page_name") or ""
                page_url = f"{self.base}/nss/p/{page_name}" if page_name else f"{self.base}/nss/p/index"
                url = f"{page_url}?id={item_id}"
                if published and (datetime.now().date() - published).days > self.lookback_days:
                    continue  # 太舊的公告（多半是封存看板）
                ext_links = [
                    (urljoin(self.base, x.get("url", "")), x.get("name") or "")
                    for x in (item.get("ext") or [])
                    if isinstance(x, dict) and x.get("url")
                ]
                notices.append(
                    Notice(
                        source_id=self.source.id,
                        school=self.source.name,
                        title=title,
                        url=url,
                        published=published,
                        content_text=text,
                        content_html=html,
                        extra={"module": module.get("name"), "module_page": page_url,
                               "ext_links": ext_links},
                    )
                )
                if len(notices) >= self.max_items:
                    break
            if len(notices) >= self.max_items:
                break

        # 附件：模組的 ext 欄位 + 內文中的檔案連結
        for n in notices:
            links = list(n.extra.get("ext_links") or [])
            links += find_attachment_links(n.content_html, self.base)
            seen: set[str] = set()
            for att_url, att_name in links:
                if att_url in seen or len(n.attachments) >= self.max_attachments:
                    continue
                seen.add(att_url)
                if self._already_known and self._already_known(n.url, att_url):
                    continue
                att = fetch_attachment(self.fetcher, att_url, att_name, self.max_attachment_bytes)
                n.attachments.append(att)

        return notices, ""

    def _match_keywords(self, title: str, text: str) -> bool:
        if any(x in title for x in self.excludes):
            return False
        blob = f"{title}\n{text}"
        return any(k in blob for k in self.keywords)

    _already_known = None
