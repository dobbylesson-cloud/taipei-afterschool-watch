"""附件下載與文字解析：PDF(pdfplumber)、DOCX(python-docx)、ODT/ODS(標準庫 zip+xml)。"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import zipfile
from xml.etree import ElementTree as ET

from .models import Attachment

log = logging.getLogger("watcher.attachments")

SUPPORTED = (".pdf", ".docx", ".odt", ".ods", ".txt")
SKIP_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".zip", ".doc", ".xls", ".xlsx")
# 舊式 .doc / .xls 為 OLE 二進位格式，此處不解析（README 已註明限制）


def guess_filename(url: str, ctype: str = "") -> str:
    path = url.split("?")[0].split("#")[0]
    name = os.path.basename(path) or "attachment"
    if not os.path.splitext(name)[1]:
        ext = ""
        if "pdf" in ctype:
            ext = ".pdf"
        elif "word" in ctype:
            ext = ".docx"
        elif "odt" in ctype or "opendocument" in ctype:
            ext = ".odt"
        name += ext
    return name


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract_text(data: bytes, filename: str) -> str:
    """依副檔名解析文字；失敗回傳空字串（由上層記錄錯誤）。"""
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".pdf":
        return _pdf_text(data)
    if ext == ".docx":
        return _docx_text(data)
    if ext in (".odt", ".ods"):
        return _odf_text(data)
    if ext == ".txt":
        return data.decode("utf-8", "ignore")
    return ""


def _pdf_text(data: bytes) -> str:
    import pdfplumber

    out = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages[:30]:  # 只取前 30 頁，避免超大檔案
            out.append(page.extract_text() or "")
    return "\n".join(out)


def _docx_text(data: bytes) -> str:
    import docx

    d = docx.Document(io.BytesIO(data))
    parts = [p.text for p in d.paragraphs]
    for table in d.tables:
        for row in table.rows:
            parts.append(" | ".join(c.text.strip() for c in row.cells))
    return "\n".join(parts)


def _odf_text(data: bytes) -> str:
    ns = {"text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0"}
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in ("content.xml",):
            if name not in z.namelist():
                continue
            root = ET.fromstring(z.read(name))
            for p in root.iter("{urn:oasis:names:tc:opendocument:xmlns:text:1.0}p"):
                txt = "".join(p.itertext()).strip()
                if txt:
                    out.append(txt)
    return "\n".join(out)


def find_attachment_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """從公告內文 HTML 找出附件連結，回傳 [(絕對網址, 顯示名稱)]。"""
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin

    soup = BeautifulSoup(html or "", "lxml")
    links: list[tuple[str, str]] = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        is_file = any(low.split("?")[0].endswith(e) for e in SUPPORTED)
        is_gov_download = "download.ashx" in low
        if not (is_file or is_gov_download):
            continue
        url = urljoin(base_url, href)
        if url in seen:
            continue
        seen.add(url)
        title = (a.get("title") or a.get_text(strip=True) or os.path.basename(href.split("?")[0]))
        links.append((url, re.sub(r"\s+", " ", title).strip()[:120]))
    return links


def fetch_attachment(fetcher, url: str, filename: str, max_bytes: int) -> Attachment:
    att = Attachment(url=url, filename=filename)
    if os.path.splitext(filename)[1].lower() in SKIP_EXTS:
        att.error = "未解析（格式不支援：圖片／舊版 .doc/.xls／壓縮檔）"
        return att
    got = fetcher.download(url, max_bytes=max_bytes)
    if not got:
        att.error = "下載失敗或超過大小上限"
        return att
    data, ctype = got
    att.sha256 = sha256_bytes(data)
    if not os.path.splitext(filename)[1]:
        filename = guess_filename(filename or url, ctype)
        att.filename = filename
    try:
        att.text = extract_text(data, att.filename)
        att.ok = bool(att.text.strip())
    except Exception as exc:
        att.error = f"{type(exc).__name__}: {exc}"
        log.info("附件解析失敗 %s：%s", att.filename, att.error)
    return att
