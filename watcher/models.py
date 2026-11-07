"""公告資料模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class Attachment:
    url: str
    filename: str
    sha256: str = ""
    text: str = ""
    ok: bool = False
    error: str = ""


@dataclass
class Notice:
    source_id: str
    school: str
    title: str
    url: str
    published: date | None = None
    content_text: str = ""
    content_html: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def searchable_text(self) -> str:
        """標題 + 內文 + 附件文字，供關鍵字比對與日期抽取。"""
        parts = [self.title, self.content_text]
        for att in self.attachments:
            if att.text:
                parts.append(att.text)
        return "\n".join(p for p in parts if p)
