"""帶逾時、重試、robots 檢查與隨機間隔的 HTTP 取用層。"""
from __future__ import annotations

import logging
import random
import time
import urllib.parse
import urllib.robotparser
from typing import Any

import requests
import urllib3

from .config import Config

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
log = logging.getLogger("watcher.http")

RETRY_STATUS = {429, 500, 502, 503, 504}


class Fetcher:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.timeout = float(cfg.get("timeout", 25))
        self.retries = int(cfg.get("retries", 3))
        self.i_min = float(cfg.get("interval_min", 1.0))
        self.i_max = float(cfg.get("interval_max", 2.0))
        self.respect_robots = bool(cfg.get("respect_robots", True))
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": cfg.get("user_agent", "Mozilla/5.0"),
                "Accept-Language": "zh-TW,zh;q=0.9",
            }
        )
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._last_request: dict[str, float] = {}
        self.stats = {"requests": 0, "failures": 0, "blocked_by_robots": 0}

    # ---------- robots ----------
    def _robots_for(self, url: str):
        host = urllib.parse.urlsplit(url).netloc
        if host in self._robots:
            return self._robots[host]
        rp = urllib.robotparser.RobotFileParser()
        robots_url = f"{urllib.parse.urlsplit(url).scheme}://{host}/robots.txt"
        try:
            resp = self.session.get(robots_url, timeout=self.timeout, verify=False)
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            else:
                rp = None  # 沒有 robots.txt 視為允許
        except Exception:
            rp = None
        self._robots[host] = rp
        return rp

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        rp = self._robots_for(url)
        if rp is None:
            return True
        return rp.can_fetch(self.session.headers["User-Agent"], url)

    # ---------- 禮貌間隔 ----------
    def _wait(self, url: str):
        host = urllib.parse.urlsplit(url).netloc
        last = self._last_request.get(host)
        if last is not None:
            need = random.uniform(self.i_min, self.i_max)
            gap = time.monotonic() - last
            if gap < need:
                time.sleep(need - gap)
        self._last_request[host] = time.monotonic()

    # ---------- 請求 ----------
    def request(self, method: str, url: str, **kwargs) -> requests.Response | None:
        if not self.allowed(url):
            self.stats["blocked_by_robots"] += 1
            log.warning("robots 禁止抓取：%s", url)
            return None
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("verify", False)
        last_err = None
        for attempt in range(1, self.retries + 1):
            self._wait(url)
            try:
                self.stats["requests"] += 1
                resp = self.session.request(method, url, **kwargs)
                if resp.status_code in RETRY_STATUS and attempt < self.retries:
                    last_err = f"HTTP {resp.status_code}"
                    time.sleep(attempt * 1.5)
                    continue
                return resp
            except Exception as exc:  # 網路錯誤
                last_err = f"{type(exc).__name__}: {exc}"
                if attempt < self.retries:
                    time.sleep(attempt * 1.5)
        self.stats["failures"] += 1
        log.warning("請求失敗（已重試 %d 次）%s：%s", self.retries, url, last_err)
        return None

    def get(self, url: str, **kwargs) -> requests.Response | None:
        return self.request("GET", url, **kwargs)

    def post_json(self, url: str, payload: Any, **kwargs) -> dict | None:
        resp = self.request("POST", url, json=payload, **kwargs)
        if resp is None or resp.status_code != 200:
            return None
        try:
            return resp.json()
        except Exception:
            log.warning("回應非 JSON：%s", url)
            return None

    def download(self, url: str, max_bytes: int | None = None) -> tuple[bytes, str] | None:
        """回傳 (內容, content-type)；超過上限則放棄。"""
        resp = self.request("GET", url, stream=True)
        if resp is None or resp.status_code != 200:
            return None
        ctype = resp.headers.get("content-type", "").lower()
        try:
            data = resp.content
        except Exception:
            return None
        if max_bytes and len(data) > max_bytes:
            log.info("附件過大略過（%.1f MB）：%s", len(data) / 1e6, url)
            return None
        return data, ctype
