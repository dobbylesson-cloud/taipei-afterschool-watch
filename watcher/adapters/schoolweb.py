"""SchoolWeb（新北市校園網站平台，/app/index.php、/p/406-… 版型）來源适配器。

流程：
1. 抓學校首頁，解析每個公告模組的 ajax 端點
   (`/app/index.php?Action=mobileloadmod&Type=mobile_xxx_mstr&Nbr=NN`)。
2. 以 POST 取得模組 HTML：
   - 若模組內容是 iframe 指向新北市教育局 `web-announce`，改用教育局公告 API
     （以學校代碼 schno 查詢）。
   - 否則解析表格 `table.listTB` 的標題／連結／日期。
3. 依關鍵字過濾標題後，再抓內頁取內容與附件。
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..attachments import fetch_attachment, find_attachment_links
from ..config import Config, Source
from ..http import Fetcher
from ..models import Notice
from ..relevance import is_relevant

log = logging.getLogger("watcher.adapters.schoolweb")

_TAB = re.compile(r'<a title="([^"]*)"[^>]*href="#(cmb_\d+_\d+)"', re.S)
_MOD = re.compile(
    r'id="(cmb_\d+_\d+)"[\s\S]{0,4000}?Action=mobileloadmod&Type=([^&\'"]+)&Nbr=(\d+)', re.S
)
_ESA = re.compile(r"web-announce/rest/service/view/public/([A-Za-z0-9+/=]+)")
_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_EXTRA_KEYS = ("家長", "學生", "課後", "社團")

ESA_API = "https://esa.ntpc.edu.tw/oauth_data/service/forall/announcepublic/select"


class SchoolWebAdapter:
    def __init__(self, cfg: Config, fetcher: Fetcher, source: Source, cache_get=None, cache_set=None):
        self.cfg = cfg
        self.fetcher = fetcher
        self.source = source
        self.base = (source.base_url or "").rstrip("/")
        self.keywords = cfg.keywords_for(source)
        self.strong_keywords = cfg.strong_keywords
        self.excludes = cfg.exclude_keywords
        self.page_keys = cfg.page_keys_for(source) + list(_EXTRA_KEYS)
        self.max_items = source.max_items or int(cfg.get("max_items", 40))
        self.lookback_days = int(cfg.get("lookback_days", 400))
        self.max_attachment_bytes = int(cfg.get("max_attachment_mb", 25)) * 1024 * 1024
        self.max_attachments = int(cfg.get("max_attachments_per_notice", 3))
        self.school_code = (source.raw or {}).get("school_code") or ""
        self._known = None
        self._already_known = None

    # ---------- 模組探索 ----------
    def _homepage(self) -> str:
        resp = self.fetcher.get(self.base + "/")
        if resp is None or resp.status_code != 200:
            return ""
        return resp.text

    def _modules(self, html: str) -> list[dict]:
        if not html:
            return []
        titles = {m.group(2): m.group(1) for m in _TAB.finditer(html)}
        mods, seen = [], set()
        for block, mtype, nbr in _MOD.findall(html):
            if block in seen:
                continue
            seen.add(block)
            title = re.sub(r"\s+", "", titles.get(block) or "")
            if title and not any(k in title for k in self.page_keys):
                continue
            mods.append({"title": title or "公告", "type": mtype, "nbr": nbr})
        return mods

    def _homepage_rows(self, html: str) -> list[dict]:
        """首頁直接渲染公告連結（/p/406-…）時的備援解析。"""
        soup = BeautifulSoup(html, "lxml")
        out, seen = [], set()
        for a in soup.select("a[href*='/p/406-']"):
            href = a.get("href") or ""
            if href in seen:
                continue
            title = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
            if len(title) < 4:
                continue
            seen.add(href)
            row = a.find_parent("tr") or a.find_parent("li") or a.parent
            dt = None
            if row is not None:
                m = _DATE.search(row.get_text(" ", strip=True))
                if m:
                    dt = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            out.append({"title": title, "url": href, "published": dt})
        return out

    def _module_html(self, mod: dict) -> str:
        url = f"{self.base}/app/index.php?Action=mobileloadmod&Type={mod['type']}&Nbr={mod['nbr']}"
        resp = self.fetcher.request(
            "POST", url, headers={"X-Requested-With": "XMLHttpRequest", "Referer": self.base + "/"}
        )
        if resp is None or resp.status_code != 200:
            return ""
        return resp.text

    # ---------- 教育局公告 API（iframe 模組）----------
    def _fetch_esa(self, schno: str, mod: str = "1") -> list[dict]:
        if not schno:
            return []
        payload = {
            "type": "request", "version": "1.0", "format": "json", "name": "查詢",
            "para": {
                "schno": schno, "search": "0", "external": "1", "method": "temopn",
                "mod": mod, "start": 0, "end": max(self.max_items, 20) - 1,
            },
            "reserved": {},
        }
        data = self.fetcher.post_json(ESA_API, payload, headers={"Referer": "https://esa.ntpc.edu.tw/"})
        if not data:
            return []
        lst = ((data.get("result") or {}).get("list")) or []
        return [x for x in lst if isinstance(x, dict) and x.get("opn_title")]

    def _esa_notice(self, row: dict) -> Notice | None:
        title = re.sub(r"\s+", " ", row.get("opn_title") or "").strip()
        text = (row.get("opn_content") or "").strip()
        html = row.get("opn_content_show") or ""
        published = _parse_date(row.get("timetext") or row.get("opn_sdate") or "")
        n = Notice(
            source_id=self.source.id, school=self.source.name, title=title,
            url=f"https://esa.ntpc.edu.tw/web-announce/rest/service/view/public/detail/{row.get('encode_id','')}",
            published=published, content_text=text, content_html=html,
        )
        return n

    # ---------- 一般表格模組 ----------
    @staticmethod
    def _rows(fragment: str) -> list[dict]:
        soup = BeautifulSoup(fragment, "lxml")
        out = []
        for tr in soup.select("table.listTB tr"):
            a = tr.find("a", href=True)
            if not a:
                continue
            tds = tr.find_all("td")
            title = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
            if not title:
                continue
            dt = None
            for td in tds:
                m = _DATE.search(td.get_text(" ", strip=True))
                if m:
                    dt = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                    break
            out.append({"title": title, "url": a["href"], "published": dt})
        return out

    def _detail(self, url: str) -> tuple[str, str, list[tuple[str, str]]]:
        resp = self.fetcher.get(url)
        if resp is None or resp.status_code != 200:
            return "", "", []
        soup = BeautifulSoup(resp.text, "lxml")
        block = soup.select_one("div.M23") or soup.select_one("div[id^='Dyn_2_']")
        if block is None:
            return "", "", []
        html = str(block)
        return html, block.get_text("\n", strip=True), find_attachment_links(html, url)

    # ---------- 主流程 ----------
    def crawl(self) -> tuple[list[Notice], str]:
        home = self._homepage()
        if not home:
            return [], "首頁無法取得"
        mods = self._modules(home)

        notices: list[Notice] = []
        seen_titles: set[str] = set()
        esa_done = False
        rows: list[dict] = []

        for mod in mods:
            if len(notices) >= self.max_items:
                break
            html = self._module_html(mod)
            if not html:
                continue

            m = _ESA.search(html)
            if m:
                if esa_done:
                    continue
                esa_done = True
                for row in self._fetch_esa(self.school_code):
                    n = self._esa_notice(row)
                    if not n or n.title in seen_titles:
                        continue
                    if n.published and (date.today() - n.published).days > self.lookback_days:
                        continue
                    if not is_relevant(n.title, n.searchable_text(), self.keywords,
                                       self.strong_keywords, self.excludes):
                        continue
                    seen_titles.add(n.title)
                    notices.append(n)
                continue

            rows.extend(self._rows(html))

        if not rows and not notices:
            rows = self._homepage_rows(home)

        for row in rows:
            if len(notices) >= self.max_items:
                break
            if row["title"] in seen_titles:
                continue
            if row["published"] and (date.today() - row["published"]).days > self.lookback_days:
                continue
            if any(x in row["title"] for x in self.excludes):
                continue
            if not any(k in row["title"] for k in self.keywords + self.strong_keywords):
                continue
            url = urljoin(self.base + "/", row["url"])
            if url.startswith("http") and self.base.split("//")[-1].split("/")[0] not in url:
                # 外部連結（表單等）只留標題
                if is_relevant(row["title"], row["title"], self.keywords,
                               self.strong_keywords, self.excludes):
                    seen_titles.add(row["title"])
                    notices.append(Notice(
                        source_id=self.source.id, school=self.source.name, title=row["title"],
                        url=url, published=row["published"],
                    ))
                continue
            detail_html, text, links = self._detail(url)
            blob = f"{row['title']}\n{text}"
            if not is_relevant(row["title"], blob, self.keywords, self.strong_keywords, self.excludes):
                continue
            seen_titles.add(row["title"])
            n = Notice(
                source_id=self.source.id, school=self.source.name, title=row["title"],
                url=url, published=row["published"], content_text=text, content_html=detail_html,
            )
            for att_url, att_name in links[: self.max_attachments]:
                if self._already_known and self._already_known(n.url, att_url):
                    continue
                n.attachments.append(
                    fetch_attachment(self.fetcher, att_url, att_name, self.max_attachment_bytes)
                )
            notices.append(n)

        if not notices:
            return [], ""
        return notices, ""


def _parse_date(text: str) -> date | None:
    m = re.search(r"(\d{2,3})\.(\d{2})\.(\d{2})", text or "")
    if m:
        y = int(m.group(1))
        if y < 1911:
            y += 1911
        try:
            return date(y, int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _DATE.search(text or "")
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None
