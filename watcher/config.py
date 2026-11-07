"""設定載入：讀取 sources/schools.yaml，提供 settings 與 sources。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class Source:
    id: str
    name: str
    type: str
    base_url: str | None = None
    list_url: str | None = None
    page_keys: list[str] | None = None
    keywords: list[str] | None = None
    max_items: int | None = None
    max_pages: int | None = None
    disabled: bool = False
    note: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class Config:
    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(ROOT, "sources", "schools.yaml")
        with open(self.path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        self.settings: dict[str, Any] = data.get("settings") or {}
        self.sources: list[Source] = []
        for item in data.get("sources") or []:
            self.sources.append(
                Source(
                    id=item["id"],
                    name=item.get("name", item["id"]),
                    type=item.get("type", "nss"),
                    base_url=item.get("base_url"),
                    list_url=item.get("list_url"),
                    page_keys=item.get("page_keys"),
                    keywords=item.get("keywords"),
                    max_items=item.get("max_items"),
                    max_pages=item.get("max_pages"),
                    disabled=bool(item.get("disabled", False)),
                    note=item.get("note", "") or "",
                    raw=item,
                )
            )

    def get(self, key: str, default=None):
        return self.settings.get(key, default)

    @property
    def keywords(self) -> list[str]:
        return list(self.settings.get("keywords") or [])

    def keywords_for(self, source: Source) -> list[str]:
        return list(source.keywords) if source.keywords else self.keywords

    @property
    def exclude_keywords(self) -> list[str]:
        return list(self.settings.get("exclude_keywords") or [])

    def page_keys_for(self, source: Source) -> list[str]:
        if source.page_keys is not None:
            return list(source.page_keys)
        return list(self.settings.get("page_keys") or [])
