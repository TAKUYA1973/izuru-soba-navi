from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "data/shops.json"
STATE = ROOT / "data/source-state.json"
STATUS = ROOT / "data/update-status.json"

JST = timezone(timedelta(hours=9))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; IzuruSobaNAVI/1.0; family-use update checker)"
}


def normalize_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()

    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def diff_summary(old: str, new: str) -> str:
    if not old:
        return "監視を開始しました。"

    old_parts = set(
        x.strip()
        for x in re.split(r"(?<=[。！？])|\s{2,}", old)
        if x.strip()
    )

    new_parts = [
        x.strip()
        for x in re.split(r"(?<=[。！？])|\s{2,}", new)
        if x.strip()
    ]

    additions = [
        x for x in new_parts
        if x not in old_parts and len(x) >= 8
    ]

    keywords = (
        "営業時間",
        "定休日",
        "休業",
        "臨時",
        "そば",
        "蕎麦",
        "メニュー",
        "円",
        "価格",
        "寒晒",
        "新そば",
        "ビール",
        "イベント",
    )

    ranked = sorted(
        additions,
        key=lambda x: (
            not any(k in x for k in keywords),
            len(x)
        )
    )

    if ranked:
        return (
            "公式ページの表示内容が変わりました。"
            "追加・変更候補: "
            + ranked[0][:160]
        )

    return (
        "公式ページの表示内容が変わりました。"
        "営業時間・メニュー等をご確認ください。"
    )


def main():
    cfg = json.loads(CFG.read_text(encoding="utf-8"))

    if STATE.exists():
        state = json.loads(STATE.read_text(encoding="utf-8"))
    else:
        state = {
            "initialized": False,
            "sources": {}
        }

    old_sources = state.get("sources", {})

    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M")

    new_sources = {}
    shop_updates = {}

    for shop, meta in cfg.items():
        slug = meta["slug"]
        best_update = None

        for url in meta["urls"]:
            try:
                r = requests.get(
                    url,
                    headers=HEADERS,
                    timeout=25
                )

                r.raise_for_status()

                text = normalize_html(r.text)

                if len(text) < 80:
                    raise ValueError("page text too short")

                current_hash = digest(text)

                prev = old_sources.get(url, {})

                new_sources[url] = {
                    "hash": current_hash,
                    "text": text,
                    "checked_at": now,
                }

                if (
                    state.get("initialized")
                    and prev.get("hash")
                    and prev["hash"] != current_hash
                ):
                    best_update = {
                        "updated": True,
                        "checked_at": now,
                        "summary": diff_summary(
                            prev.get("text", ""),
                            text
                        ),
                        "source_url": url,
                    }

            except Exception as e:
                if url in old_sources:
                    new_sources[url] = old_sources[url]

                print(
                    f"[WARN] {shop} {url}: {e}",
                    file=sys.stderr
                )

        if best_update:
            shop_updates[slug] = best_update

    STATE.write_text(
        json.dumps(
            {
                "initialized": True,
                "sources": new_sources
            },
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    STATUS.write_text(
        json.dumps(
            {
                "last_checked": now,
                "shops": shop_updates
            },
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    print(
        f"checked {len(new_sources)} sources; "
        f"updates={len(shop_updates)}"
    )


if __name__ == "__main__":
    main()
