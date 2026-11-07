"""公告相關性判斷。

弱關鍵字（社團、才藝）很容易在公文範本、研習公告裡誤中，
所以改成兩層：強關鍵字直接命中即收；弱關鍵字必須出現在標題，
且全文另有「報名／招生／申請」等行為詞才算相關。
"""
from __future__ import annotations

# 出現在全文任一處，代表這確實是報名/招生類公告
APPLY_HINTS = (
    "報名", "招生", "申請", "簡章", "報名表", "開課", "錄取", "抽籤", "繳費",
)


def is_relevant(
    title: str,
    blob: str,
    keywords: list[str],
    strong_keywords: list[str] | None = None,
    excludes: list[str] | None = None,
) -> bool:
    title = title or ""
    blob = blob or ""

    if any(x and x in title for x in (excludes or [])):
        return False

    if any(k and k in blob for k in (strong_keywords or [])):
        return True

    if not any(k and k in title for k in keywords):
        return False

    return any(h in blob for h in APPLY_HINTS)
