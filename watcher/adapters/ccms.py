"""CCMS（臺北市政府共用網站平台，教育局與少數學校）來源适配器。

列表頁為伺服器端渲染的表格（td.CCMS_jGridView_td_*），內頁內容在 #CCMS_Content，
附件連結為 Download.ashx 或直接檔案網址。
"""
from __future__ import annotations

import logging
import re
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

from bs4 import BeautifulSoup

from ..attachments import fetch_attachment, find_attachment_links
from ..config import Config, Source
from ..http import Fetcher
from ..models import Notice
from ..relevance import is_relevant

log = logging.getLogger("watcher.adapters.ccms")

_DATE_RE = re.compile(r"(\d{2,3})-(\d{2})-(\d{2})")


def _with_page(url: str, page: int, page_size: int = 20) -> str:
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q["page"] = str(page)
    q["PageSize"] = str(page_size)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


def _parse_row_date(text: str) -> date | None:
    m = _DATE_RE.search(text)
    if not m:
        return None
    y = int(m.group(1))
    if y < 1911:
        y += 1911
    try:
        return date(y, int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


class CCMSAdapter:
    def __init__(self, cfg: Config, fetcher: Fetcher, source: Source, cache_get=None, cache_set=None):
        self.cfg = cfg
        self.fetcher = fetcher
        self.source = source
        self.list_url = source.list_url
        self.keywords = cfg.keywords_for(source)
        self.strong_keywords = cfg.strong_keywords
        self.excludes = cfg.exclude_keywords
        self.max_pages = source.max_pages or int(cfg.get("max_pages", 2))
        self.max_items = source.max_items or int(cfg.get("max_items", 40))
        self.lookback_days = int(cfg.get("lookback_days", 400))
        self.max_attachment_bytes = int(cfg.get("max_attachment_mb", 25)) * 1024 * 1024
        self.max_attachments = int(cfg.get("max_attachments_per_notice", 3))
        self._known = None

    def _list_entries(self) -> list[dict]:
        entries: list[dict] = []
        seen: set[str] = set()
        for page in range(1, self.max_pages + 1):
            resp = self.fetcher.get(_with_page(self.list_url, page))
            if resp is None or resp.status_code != 200:
                break
            soup = BeautifulSoup(resp.text, "lxml")
            page_rows = 0
            for tr in soup.find_all("tr"):
                a = tr.find("a", href=lambda h: h and "News_Content.aspx" in h and "s=" in h)
                if not a:
                    continue
                href = urljoin(self.list_url, a["href"])
                if href in seen:
                    continue
                title = re.sub(r"\s+", " ", (a.get("title") or a.get_text(" ", strip=True) or "")).strip()
                if not title or title in ("申辦資訊",):
                    continue
                seen.add(href)
                entries.append(
                    {
                        "url": href,
                        "title": title,
                        "published": _parse_row_date(tr.get_text(" ", strip=True)),
                    }
                )
                page_rows += 1
            if page_rows == 0:
                break
        return entries

    def _detail(self, url: str) -> tuple[str, str, list[tuple[str, str]]]:
        resp = self.fetcher.get(url)
        if resp is None or resp.status_code != 200:
            return "", "", []
        soup = BeautifulSoup(resp.text, "lxml")
        block = soup.select_one("#CCMS_Content") or soup.select_one(".page-content") or soup.body
        html = str(block) if block else ""
        text = block.get_text("\n", strip=True) if block else ""
        return html, text, find_attachment_links(html, url)

    def crawl(self) -> tuple[list[Notice], str]:
        entries = self._list_entries()
        if not entries:
            return [], "列表頁沒有解析到任何公告（版面可能改版或被阻擋）"

        notices: list[Notice] = []
        for e in entries:
            if e["published"] and (date.today() - e["published"]).days > self.lookback_days:
                continue
            if any(x in e["title"] for x in self.excludes):
                continue
            if not any(k in e["title"] for k in self.keywords + self.strong_keywords):
                # 標題沒命中就先跳過，避免為了全部公告打內頁
                continue
            html, text, links = self._detail(e["url"])
            blob = f"{e['title']}\n{text}"
            if not is_relevant(
                e["title"], blob, self.keywords, self.strong_keywords, self.excludes
            ):
                continue
            n = Notice(
                source_id=self.source.id,
                school=self.source.name,
                title=e["title"],
                url=e["url"],
                published=e["published"],
                content_text=text,
                content_html=html,
            )
            for att_url, att_name in links[: self.max_attachments]:
                if self._known and self._known(n.url, att_url):
                    continue
                n.attachments.append(
                    fetch_attachment(self.fetcher, att_url, att_name, self.max_attachment_bytes)
                )
            notices.append(n)
            if len(notices) >= self.max_items:
                break
        return notices, ""
