#!/usr/bin/env python3
"""每日執行入口：爬取 → 抽取 → 存檔 → 重新產生 index.html。

用法：
  python run_daily.py                     # 跑設定的全部來源
  python run_daily.py --only meps,japs    # 只跑指定來源（除錯用）
  python run_daily.py --no-attachments    # 不下載附件（快速模式）
  python run_daily.py --max-items 10      # 覆寫每個來源處理筆數
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
import traceback
from datetime import date

from watcher.adapters import ADAPTERS
from watcher.config import Config
from watcher.extractor import extract_period
from watcher.http import Fetcher
from watcher.renderer import build_rows, render
from watcher.storage import Store

ROOT = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("run_daily")


def content_hash(notice) -> str:
    parts = [notice.title, notice.content_text or ""]
    parts += [a.sha256 or a.filename for a in notice.attachments]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="臺北市國小課後社團公告監測")
    ap.add_argument("--config", default=os.path.join(ROOT, "sources", "schools.yaml"))
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "watch.db"))
    ap.add_argument("--out", default=os.path.join(ROOT, "index.html"))
    ap.add_argument("--only", default="", help="只跑這些來源 id（逗號分隔）")
    ap.add_argument("--no-attachments", action="store_true")
    ap.add_argument("--max-items", type=int, default=0)
    ap.add_argument(
        "--reextract-only",
        action="store_true",
        help="不重新爬取，只用資料庫既有內容重跑日期抽取並重新產生 index.html",
    )
    args = ap.parse_args()

    cfg = Config(args.config)
    if args.no_attachments:
        cfg.settings["max_attachments_per_notice"] = 0

    store = Store(args.db)
    today = date.today()

    # 只重跑抽取（調整規則後不必重新爬全站）
    if args.reextract_only:
        items = store.notices_for_reextract()
        for it in items:
            pub = date.fromisoformat(it["published"]) if it["published"] else None
            store.save_extraction(it["url"], extract_period(it["text"], pub))
        last = store.last_run() or {}
        sources_meta = [
            {"id": s["source_id"], "name": s["name"], "status": s["status"], "error": s["error"] or ""}
            for s in (last.get("sources") or [])
        ]
        rows = build_rows(store.rows_for_render(), today)
        render(args.out, rows, sources_meta, last, store.count_notices(), today)
        print(f"重新抽取完成：{len(items)} 筆，已重新產生 {args.out}")
        store.close()
        return 0

    fetcher = Fetcher(cfg)
    run_id = store.start_run()

    wanted = {s.strip() for s in args.only.split(",") if s.strip()}
    sources_meta: list[dict] = []
    new_count = updated_count = 0
    ok_sources = failed_sources = 0

    for src in cfg.sources:
        if wanted and src.id not in wanted:
            continue
        if src.disabled:
            log.info("略過 %s（停用：%s）", src.name, src.note or "未註明")
            store.log_source(run_id, src.id, src.name, "disabled", 0, src.note)
            sources_meta.append({"id": src.id, "name": src.name, "status": "disabled", "error": src.note})
            continue

        adapter_cls = ADAPTERS.get(src.type)
        if not adapter_cls:
            store.log_source(run_id, src.id, src.name, "error", 0, f"未知來源型別 {src.type}")
            sources_meta.append({"id": src.id, "name": src.name, "status": "error", "error": f"未知型別 {src.type}"})
            failed_sources += 1
            continue

        log.info("開始抓取 %s（%s）", src.name, src.type)
        adapter = adapter_cls(cfg, fetcher, src, cache_get=store.get_meta, cache_set=store.set_meta)
        adapter._known = store.known_attachment
        adapter._already_known = store.known_attachment
        if args.max_items:
            adapter.max_items = args.max_items

        try:
            notices, error = adapter.crawl()
        except Exception as exc:  # 單一來源失敗不影響整批
            error = f"{type(exc).__name__}: {exc}"
            log.error("來源失敗 %s：%s", src.name, error)
            log.debug(traceback.format_exc())
            notices = []

        if error and not notices:
            failed_sources += 1
            store.log_source(run_id, src.id, src.name, "error", 0, error)
            sources_meta.append({"id": src.id, "name": src.name, "status": "error", "error": error})
            continue

        ok_sources += 1
        store.log_source(run_id, src.id, src.name, "ok", len(notices), error or "")
        sources_meta.append({"id": src.id, "name": src.name, "status": "ok", "error": error or "", "found": len(notices)})

        for n in notices:
            is_new, changed = store.upsert_notice(n, content_hash(n))
            for att in n.attachments:
                store.upsert_attachment(n.url, att)
            # 抽取：標題 + 內文 + 附件文字
            res = extract_period(n.searchable_text(), n.published)
            store.save_extraction(n.url, res)
            if is_new:
                new_count += 1
            elif changed:
                updated_count += 1
            log.info(
                "  %s｜%s → %s ~ %s（%s, 信心 %.2f）",
                "新" if is_new else "舊", n.title[:34],
                res.start or "?", res.end or "?", res.rule, res.confidence,
            )

    total = store.count_notices()
    store.finish_run(
        run_id,
        new_count=new_count,
        updated_count=updated_count,
        total_count=total,
        ok_sources=ok_sources,
        failed_sources=failed_sources,
        note=f"requests={fetcher.stats['requests']} failures={fetcher.stats['failures']}",
    )

    rows = build_rows(store.rows_for_render(), today)
    render(args.out, rows, sources_meta, store.last_run(), total, today)

    print("\n" + "=" * 66)
    print("執行完成")
    print(f"  來源成功 {ok_sources}／失敗 {failed_sources}／停用 {len([s for s in sources_meta if s['status']=='disabled'])}")
    print(f"  本輪新增公告 {new_count} 筆，內容更新 {updated_count} 筆，資料庫共 {total} 筆")
    print(f"  儀表板：{args.out}")
    print(f"  HTTP 請求 {fetcher.stats['requests']} 次、失敗 {fetcher.stats['failures']} 次、"
          f"robots 阻擋 {fetcher.stats['blocked_by_robots']} 次")
    status = {s["id"]: s for s in sources_meta}
    bad = [s for s in sources_meta if s["status"] == "error"]
    if bad:
        print("  失敗來源：")
        for s in bad:
            print(f"    - {s['name']}（{s['id']}）：{s['error']}")
    live = sum(1 for r in rows if r["status"] == "進行中")
    soon = sum(1 for r in rows if r["status"] == "即將開始")
    unk = sum(1 for r in rows if r["status"] == "日期未識別")
    print(f"  儀表板統計：進行中 {live}、即將開始 {soon}、日期未識別 {unk}")
    print("=" * 66)

    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
