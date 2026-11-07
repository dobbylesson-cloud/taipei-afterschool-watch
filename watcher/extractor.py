"""規則式「申請期間」抽取。

支援：
  113年3月1日、113/03/01、2024-03-01（民國與西元混用）
  113年3月1日至113年3月15日 / 113年3月1日至3月15日（結束日期省略年份）
  3月1日至3月15日（兩端都省略年份，用上下文年分推斷）
  「自…起至…止」「報名期間：…～…」等常見寫法

限制：這是啟發法，誤抓／漏抓都可能發生，輸出必帶原文片段供人工複核。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

# ---------- 文字正規化 ----------

_FW_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_PUNCT_MAP = {
    "～": "~", "〜": "~", "－": "-", "—": "-", "–": "-", "‑": "-", "‒": "-",
    "　": " ", "\u00a0": " ", "：": ":", "，": ",", "（": "(", "）": ")",
}
_WEEKDAY = re.compile(r"(?:[(（]\s*(?:星期|週)[一二三四五六日天]\s*[)）])|(?:[(（]\s*[一二三四五六日天]\s*[)）])")
_ROC_WORD = re.compile(r"民國")

# 中文數字日期（三月一日、十二月十五日）→ 阿拉伯數字
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_DATE_WORDS = re.compile(
    r"([零〇一二兩三四五六七八九十]{1,3})\s*月\s*([零〇一二兩三四五六七八九十]{1,3})\s*日"
)


def _cn_number(s: str) -> int | None:
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if "十" in s:
        head, _, tail = s.partition("十")
        tens = _CN_DIGITS.get(head, 1) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        return tens * 10 + ones
    return _CN_DIGITS.get(s)


def _cn_dates_to_digits(text: str) -> str:
    def repl(m: re.Match) -> str:
        month = _cn_number(m.group(1))
        day = _cn_number(m.group(2))
        if month and day:
            return f"{month}月{day}日"
        return m.group(0)

    return _CN_DATE_WORDS.sub(repl, text)


def normalize(text: str) -> str:
    if not text:
        return ""
    text = text.translate(_FW_DIGITS)
    for k, v in _PUNCT_MAP.items():
        text = text.replace(k, v)
    text = _ROC_WORD.sub("", text)
    text = _WEEKDAY.sub("", text)
    text = _cn_dates_to_digits(text)
    text = re.sub(r"[ \t]+", " ", text)
    return text


# ---------- 日期樣式 ----------

_CN_DATE = (
    r"(?:(?P<y{n}>\d{{2,4}})\s*年\s*)?"
    r"(?P<m{n}>\d{{1,2}})\s*月\s*(?P<d{n}>\d{{1,2}})\s*日?"
)
_SEP = r"(?:起|開始|上午|下午)?\s*(?:至|到|~|-|迄|起至|起迄)\s*(?:止|截止)?"
_CN_RANGE = re.compile(
    _CN_DATE.format(n=1) + r"\s*(?:起)?\s*(?:至|到|~|-|迄)\s*" + _CN_DATE.format(n=2) + r"\s*(?:止|截止|前)?"
)

_NUM_DATE = r"(?:(?P<y{n}>\d{{2,4}})\s*[/.-]\s*)?(?P<m{n}>\d{{1,2}})\s*[/.-]\s*(?P<d{n}>\d{{1,2}})"
_NUM_RANGE = re.compile(
    r"(?<!\d)" + _NUM_DATE.format(n=1) + r"\s*(?:~|至|到|-|迄)\s*" + _NUM_DATE.format(n=2) + r"(?!\d)"
)

_ROC_YEAR_IN_TEXT = re.compile(r"(?<!\d)(\d{2,3})\s*年")

# 「即日起至 3 月 8 日」：開始日以公告日推算
_OPEN_START = re.compile(
    r"(?:即日起|自即日起|公告日起|本日起)\s*(?:至|到|~|-)\s*" + _CN_DATE.format(n=1)
)

# ---------- 只有單一日期（沒有區間）的情況 ----------
# 「115年11月13日止」「115/11/30 前報名」→ 只有截止日
_ONE_CN = _CN_DATE.format(n=9)
_ONE_NUM = r"(?<!\d)" + _NUM_DATE.format(n=9) + r"(?!\d)"
_TRAIL = r"\s*(?:\([^)]{0,10}\))?\s*"
# 日期與「前/止/截止」之間可能夾「中午12:00」「下班前」等字樣
_TRAIL_WIDE = r"[^。;;\n]{0,14}?"
_DEADLINE_CN = re.compile(_ONE_CN + _TRAIL_WIDE + r"(?:前|止|截止)")
_DEADLINE_NUM = re.compile(_ONE_NUM + _TRAIL_WIDE + r"(?:前|止|截止)")
# 「截止日：115年11月20日」：截止字樣在日期前面
_DEADLINE_BEFORE_CN = re.compile(
    r"(?:截止|止)\s*(?:日|日期|時間|期限)?\s*:?\s*" + _ONE_CN
)
_DEADLINE_BEFORE_NUM = re.compile(
    r"(?:截止|止)\s*(?:日|日期|時間|期限)?\s*:?\s*" + _ONE_NUM
)
# 「報名開放日：115年10月23日」→ 只有開始日
_OPEN_LABEL = r"(?:報名開放|開放報名|報名開始|報名首日|接受報名|報名日期|報名時間)"
_OPEN_SINGLE_CN = re.compile(_OPEN_LABEL + r"\s*(?:日|時間)?\s*:?\s*" + _ONE_CN)
_OPEN_SINGLE_NUM = re.compile(_OPEN_LABEL + r"\s*(?:日|時間)?\s*:?\s*" + _ONE_NUM)
# 只有截止日時，前後出現這些詞才算「報名截止」而不是活動結束
_DEADLINE_WORDS = (
    "報名", "申請", "受理", "登記", "招生", "截止", "期限",
    "繳交", "上傳", "送件", "寄件", "回覆",
)

# 出現在日期區間前後、可用來判斷「這是報名/申請期間」的詞
_APPLY_WORDS = (
    "報名期間", "報名日期", "報名時間", "報名", "申請期間", "申請日期", "申請",
    "受理", "登記", "招生", "期限", "截止", "作業期間",
)
# 若日期區間前面緊接著這些標籤，代表是活動/上課日期，不是申請期間
_EXCLUDE_LABELS = (
    "活動日期", "活動時間", "營隊日期", "課程日期", "上課日期", "適用時間",
    "展覽日期", "辦理日期", "演出日期", "比賽日期",
)
# 出現這些詞時多半不是申請期間（面試、考試、放榜…）
_NEGATIVE_WORDS = ("面試", "口試", "筆試", "測驗", "考試", "甄試", "放榜", "錄取公告", "說明會", "成果發表")

# 沒寫年份的日期若比公告日早超過這個天數，視為隔年
_BACKSTEP = timedelta(days=30)


@dataclass
class Extracted:
    start: date | None
    end: date | None
    evidence: str = ""
    confidence: float = 0.0
    rule: str = ""
    apply_ctx: bool = False

    @property
    def ok(self) -> bool:
        return self.start is not None and self.end is not None


def _to_ad(raw: str | None, fallback: int) -> int:
    """把抓到的年份字串轉成西元；2~3 位視為民國年。"""
    if not raw:
        return fallback
    v = int(raw)
    if v <= 1911:  # 民國年（含 3 位數的 100+ 年）
        return v + 1911
    return v


def _context_year(text: str, published: date | None) -> int:
    """優先取文中最近一次提到的民國年，否則用公告發布年，最後用今年。"""
    m = _ROC_YEAR_IN_TEXT.search(text)
    if m:
        v = int(m.group(1))
        if 1 <= v <= 200:
            return v + 1911
        if 1911 < v < 2100:
            return v
    if published:
        return published.year
    return date.today().year


def _make_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _snippet(text: str, start: int, end: int, pad: int = 48) -> str:
    s = max(0, start - pad)
    e = min(len(text), end + pad)
    frag = re.sub(r"\s+", " ", text[s:e]).strip()
    return frag


def _apply_context(text: str, start: int, end: int) -> bool:
    """日期區間前後是否出現「報名/申請/受理…」等字樣；沒有就視為一般活動日期。"""
    before = text[max(0, start - 30):start]
    after = text[end:end + 14]
    if any(lbl in before for lbl in _EXCLUDE_LABELS):
        return False
    return any(w in before for w in _APPLY_WORDS) or any(w in after for w in _APPLY_WORDS)


def _score(text: str, start: int, end: int, both_years: bool, has_qi_zhi: bool) -> float:
    before = text[max(0, start - 30):start]
    after = text[end:end + 12]
    score = 0.0
    if any(w in before for w in _APPLY_WORDS):
        score += 3.0
    if any(w in after for w in ("報名", "申請", "止", "截止")):
        score += 0.5
    if both_years:
        score += 2.0
    if has_qi_zhi:
        score += 1.0
    if "期間" in before or "日期" in before:
        score += 1.0
    if any(w in before for w in _NEGATIVE_WORDS):
        score -= 2.0
    if any(w in after for w in _NEGATIVE_WORDS):
        score -= 1.0
    return score


def _parse_single(m: re.Match, ctx_year: int, published: date | None) -> date | None:
    """從 n=9 的單一日期 regex 取得日期；沒寫年份且早於公告日過多則視為隔年。"""
    raw_y = m.group("y9")
    month = m.group("m9")
    day = m.group("d9")
    if month is None or day is None:
        return None
    year = _to_ad(raw_y, ctx_year)
    dt = _make_date(year, int(month), int(day))
    if dt and raw_y is None and published and dt < published - _BACKSTEP:
        dt = _make_date(year + 1, int(month), int(day))
    return dt


def extract_period(raw_text: str, published: date | None = None) -> Extracted:
    text = normalize(raw_text)
    if not text:
        return Extracted(None, None, "", 0.0, "no-text")

    candidates: list[Extracted] = []
    ctx_year = _context_year(text, published)

    for m in _CN_RANGE.finditer(text):
        y1 = m.group("y1")
        y2 = m.group("y2")
        start_year = _to_ad(y1, ctx_year)
        start = _make_date(start_year, int(m.group("m1")), int(m.group("d1")))
        if start and y1 is None and published and start < published - _BACKSTEP:
            # 沒寫年份且日期明顯早於公告日 → 應是隔年
            start_year += 1
            start = _make_date(start_year, int(m.group("m1")), int(m.group("d1")))
        end = _make_date(_to_ad(y2, start_year), int(m.group("m2")), int(m.group("d2")))
        if not start or not end:
            continue
        if y2 is None and end < start:
            end = _make_date(end.year + 1, int(m.group("m2")), int(m.group("d2")))
        if end is None or end < start:
            continue
        candidates.append(
            Extracted(
                start=start,
                end=end,
                evidence=_snippet(text, m.start(), m.end()),
                confidence=_score(text, m.start(), m.end(), bool(y1 and y2), "止" in m.group(0)),
                rule="cn-range",
                apply_ctx=_apply_context(text, m.start(), m.end()),
            )
        )

    for m in _NUM_RANGE.finditer(text):
        y1 = m.group("y1")
        y2 = m.group("y2")
        start_year = _to_ad(y1, ctx_year)
        start = _make_date(start_year, int(m.group("m1")), int(m.group("d1")))
        if start and y1 is None and published and start < published - _BACKSTEP:
            start_year += 1
            start = _make_date(start_year, int(m.group("m1")), int(m.group("d1")))
        end = _make_date(_to_ad(y2, start_year), int(m.group("m2")), int(m.group("d2")))
        if not start or not end:
            continue
        if y2 is None and end < start:
            end = _make_date(end.year + 1, int(m.group("m2")), int(m.group("d2")))
        if end is None or end < start:
            continue
        candidates.append(
            Extracted(
                start=start,
                end=end,
                evidence=_snippet(text, m.start(), m.end()),
                confidence=_score(text, m.start(), m.end(), bool(y1 and y2), False),
                rule="num-range",
                apply_ctx=_apply_context(text, m.start(), m.end()),
            )
        )

    # 「即日起至 3 月 8 日」：開始日以公告日推算
    for m in _OPEN_START.finditer(text):
        if published is None:
            continue
        end = _make_date(_to_ad(m.group("y1"), ctx_year), int(m.group("m1")), int(m.group("d1")))
        if not end:
            continue
        if end < published and m.group("y1") is None:  # 沒寫年份且落在公告日之前 → 隔年
            end = _make_date(end.year + 1, int(m.group("m1")), int(m.group("d1")))
        if end is None or end <= published:
            continue
        candidates.append(
            Extracted(
                start=published,
                end=end,
                evidence=_snippet(text, m.start(), m.end()),
                confidence=3.5,
                rule="open-start",
                apply_ctx=_apply_context(text, m.start(), m.end()),
            )
        )

    # 只有截止日（「115年11月13日止」「115/11/30 前報名」）
    for pat, rule in (
        (_DEADLINE_CN, "deadline-only"),
        (_DEADLINE_NUM, "deadline-only-num"),
        (_DEADLINE_BEFORE_CN, "deadline-before"),
        (_DEADLINE_BEFORE_NUM, "deadline-before-num"),
    ):
        for m in pat.finditer(text):
            end = _parse_single(m, ctx_year, published)
            if not end:
                continue
            before = text[max(0, m.start() - 40):m.start()]
            after = text[m.end():m.end() + 20]
            if any(lbl in before for lbl in _EXCLUDE_LABELS):
                continue
            if not any(w in before + after for w in _DEADLINE_WORDS):
                continue
            # 「即日起…截止」：開始日以公告日推算
            start = None
            if published and re.search(r"(即日起|本日起|公告日起)", before):
                start = published
            candidates.append(
                Extracted(
                    start=start,
                    end=end,
                    evidence=_snippet(text, m.start(), m.end()),
                    confidence=max(_score(text, m.start(), m.end(), False, False), 3.5),
                    rule=rule,
                    apply_ctx=True,
                )
            )

    # 只有開始日（「報名開放日：115年10月23日」）
    for pat, rule in ((_OPEN_SINGLE_CN, "open-only"), (_OPEN_SINGLE_NUM, "open-only-num")):
        for m in pat.finditer(text):
            start = _parse_single(m, ctx_year, published)
            if not start:
                continue
            candidates.append(
                Extracted(
                    start=start,
                    end=None,
                    evidence=_snippet(text, m.end() - 20, m.end()),
                    confidence=max(_score(text, m.start(), m.end(), False, False), 3.5),
                    rule=rule,
                    apply_ctx=True,
                )
            )

    # 只保留「看起來是報名/申請期間」的候選，避免抓到活動日期或繳費區間
    if not candidates:
        return Extracted(None, None, "", 0.0, "no-match")

    applied = [c for c in candidates if c.apply_ctx]
    if not applied:
        return Extracted(None, None, "", 0.0, "no-apply-context")

    def _rank(c: Extracted):
        anchor = c.start or c.end
        return (1 if c.ok else 0, c.confidence, -anchor.toordinal() if anchor else 0)

    # 完整區間優先，其次比信心分數；同分取較早出現者
    best = max(applied, key=_rank)
    best.confidence = max(0.0, min(0.95, round(best.confidence / 7.0, 2)))
    return best


def extract_single_dates(raw_text: str) -> list[date]:
    """備援：抽取文中所有單一日期（供人工核對時參考）。"""
    text = normalize(raw_text)
    out: list[date] = []
    ctx = _context_year(text, None)
    for m in re.finditer(_CN_DATE.format(n=1), text):
        d = _make_date(_to_ad(m.group("y1"), ctx), int(m.group("m1")), int(m.group("d1")))
        if d:
            out.append(d)
    return out
