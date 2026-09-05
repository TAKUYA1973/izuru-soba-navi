from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

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

# 栃木市観光協会サイトの各スポットページは、末尾に「同じカテゴリーのスポット」という
# おすすめ店舗の一覧（アクセスの度に内容が変わる）と、全ページ共通のフッターが続く。
# 店舗自身の情報とは無関係にここが変化するだけで更新検知が誤発火するため、比較対象から除外する。
TOCHIGI_KANKOU_HOST = "tochigi-kankou.or.jp"
TOCHIGI_KANKOU_CORE_START = "スポット トップページ"
TOCHIGI_KANKOU_CORE_END = "同じカテゴリーのスポット"


def normalize_html(html: bytes | str) -> str:
    # r.content（バイト列）をそのまま渡すことで、BeautifulSoup 自身に
    # 文字コードを判定させる。requests の r.text は charset ヘッダが
    # 無い/誤っているサイトで ISO-8859-1 に誤判定し文字化けすることがあるため。
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()

    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def is_tochigi_kankou_url(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return host.endswith(TOCHIGI_KANKOU_HOST)


def extract_core_content(text: str, url: str) -> str:
    """店舗と無関係な変動部分（関連スポット一覧・共通ナビ/フッター）を取り除き、
    所在地・営業時間・定休日・メニュー・価格など店舗固有の情報だけを比較対象にする。"""

    if not is_tochigi_kankou_url(url):
        return text

    start = text.find(TOCHIGI_KANKOU_CORE_START)
    start = start + len(TOCHIGI_KANKOU_CORE_START) if start != -1 else 0

    end = text.find(TOCHIGI_KANKOU_CORE_END)

    if end != -1 and end > start:
        return text[start:end].strip()

    if start > 0:
        return text[start:].strip()

    return text


def canonical_form(text: str) -> str:
    """カテゴリータグ等、表示順が読み込みごとに入れ替わることがある単語列を
    順序に依存せず比較できるよう、単語集合として正規化する。

    単語の「集合」でしか比較できなくなる（＝どの単語がどの単語と対応しているか
    が失われる）ため、価格やメニュー名が入れ替わったような意味のある変更まで
    見逃すおそれがある。そのため、実際に表示順の入れ替わりが確認されている
    観光協会ページの店舗情報ブロックに限定して使用すること。
    """
    return " ".join(sorted(text.split()))


def digest(text: str, url: str) -> str:
    hash_source = canonical_form(text) if is_tochigi_kankou_url(url) else text
    return hashlib.sha256(hash_source.encode("utf-8")).hexdigest()


# 営業時間・定休日の値を切り出す際、次にこのいずれかの語が現れたところで
# 値の切り出しを止める。各公式URLの実際のキャッシュ済みテキスト
# (data/source-state.json) を確認し、そこに実在するラベル・見出し語から
# 組み立てている(推測だけで作ったものではない)。
STOP_LABELS = [
    "定休日", "営業時間", "専用駐車場", "アクセス方法", "公式WEB",
    "所在地", "住所", "TEL", "FAX", "電話", "E-MAIL",
    "メニュー", "価格", "料金",
    "大型バス駐車場", "同じカテゴリーのスポット",
    "最寄駅より", "アレルギー物質について",
    "Instagram", "Facebook", "Contact", "Copyright",
    "PAGE TOP", "ホーム", "トップへ戻る", "トップページ",
    "配送/支払い条件", "サイトマップ", "プライバシーポリシー",
    "地図を印刷", "URLをコピー", "ログアウト",
]

_LABEL_SEP = r"\s*[:：]?\s*"
_PRICE_RE = re.compile(r"[+＋]?\d[\d,]*\s*円")
_MENU_HEADING_RE = re.compile(r"メニュー|お\s*品\s*書\s*き")

# いづるやの実メニューページ(iduruya.co.jp/menu/)を実データで確認したところ、
# 価格の無い季節限定品の案内文言「店内告知をご覧ください」がそのまま次の
# 商品名に混入する実例があった。汎用的な案内文言なので取り除く。
_MENU_FILLER_PHRASES = ["店内告知をご覧ください"]


def _normalize_value(value: str) -> str:
    """表記揺れ（全角/半角、空白、改行）を吸収するための正規化。
    これを経ることで、表記が変わっただけの差分を実質的な変更として
    検知しないようにする。"""
    # NFKC では「〜」(波ダッシュ U+301C) は「~」(全角チルダ U+FF5E)に
    # 正規化されない。見た目がほぼ同じで、時間表記(11:00〜18:00等)で
    # サイトによって使い分けが揺れるため、先に統一しておく。
    value = value.replace("〜", "~").replace("～", "~")
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value.strip(" 　:：・／/,-")


def extract_labeled_field(text: str, label: str) -> str | None:
    """text の中から「<label> <値>」の形になっている値を取り出す。
    label が見つからない場合や、値が空になる場合は None を返し、
    「確実に抽出できない場合は変更と断定しない」の方針に従う。"""

    match = re.search(re.escape(label) + _LABEL_SEP, text)
    if not match:
        return None

    start = match.end()
    end = len(text)
    for stop in STOP_LABELS:
        if stop == label:
            continue
        idx = text.find(stop, start)
        if idx != -1 and idx < end:
            end = idx

    value = _normalize_value(text[start:end])
    # 観光協会ページ末尾に付く内部スポットID（例:「金曜日 21」の"21"）を除去する。
    value = re.sub(r"\s+\d+$", "", value).strip()
    return value or None


def extract_menu_items(text: str) -> dict[str, str] | None:
    """text からメニュー名と価格の対応を抽出する。

    「メニュー」「お品書き」のような見出し語が一切見つからないページでは
    そもそも試みず None を返す(＝未実施。抽出できないものを無理に埋めない)。
    見出し語はあるが価格(◯◯円)が1件も見つからない場合は空の辞書を返す。

    価格の直前3語を商品名とみなす。ページ全体を1つの空白区切り文字列として
    扱っているため、直前の価格からこの価格までの間の語をそのまま商品名にすると、
    最初の商品ではナビゲーションや前置きの文章まで巻き込んでしまう。
    直前3語に絞ることでその混入を抑えるが、4語以上の商品名は末尾3語に
    切り詰められる場合がある(既知の制限)。

    また実データ(いづるやの実メニューページ)で以下の2パターンの混入を
    確認したため、直前3語を取り出す前に軽減している。
    - 価格の無い案内文が句点「。」で終わり、その直後に次の商品名が続く
      (例:「…ご賞味ください。 そばがき 950円」)場合、最後の句点より前は
      捨てて句点より後ろだけを対象にする。
    - 「店内告知をご覧ください」のような、価格の無い季節限定品の案内文言。

    これでも「冷たいおそば もりそば」のように、価格の無い短い見出し語
    (2〜4文字程度)が直前にそのまま連結される場合まではDOM構造が無いと
    確実には除去できない(既知の制限。値そのものは安定しており、価格変更の
    検知漏れ・誤検知は起きない)。
    """

    if not _MENU_HEADING_RE.search(text):
        return None

    matches = list(_PRICE_RE.finditer(text))
    if not matches:
        return {}

    items: dict[str, str] = {}
    prev_end = 0
    for m in matches:
        gap = text[prev_end:m.start()]
        if "。" in gap:
            gap = gap.rsplit("。", 1)[1]
        for phrase in _MENU_FILLER_PHRASES:
            gap = gap.replace(phrase, " ")

        gap_tokens = gap.split()
        name = _normalize_value(" ".join(gap_tokens[-3:]))
        price = _normalize_value(m.group(0))
        if name and price:
            items[name] = price
        prev_end = m.end()

    return items


# 「出流ふれあいの森」のページ(そば処やまぶきが園内で営業)は、実データを
# 確認したところ「営業時間 8:30~17:00 ※そば店「やまぶき」は…営業11:00~14:00」
# のように、公園全体の営業時間とやまぶき自身の営業時間が1つの文字列に
# 混在している。DOM構造が無いテキストからやまぶき固有の値だけを安全に
# 切り出す方法が無いため、公園側の営業時間・定休日が変わっただけで
# 「やまぶきの営業時間/定休日が変わった」と誤って表示してしまう恐れがある。
# そのため、このURLに限り営業時間/定休日の構造化差分検知を無効化する
# (ページ全体の更新検知自体はPR#1の仕組みのまま維持され、更新は
# 従来通りの固定文言で表示される)。
STRUCTURED_HOURS_CLOSED_EXCLUDED_URLS = {
    "https://www.tochigi-kankou.or.jp/spot/izuru-fureainomori",
}


def extract_structured_fields(text: str, url: str | None = None) -> dict:
    """比較・表示用に、店舗固有の構造化データ(営業時間/定休日/メニューと価格)
    を抽出する。text には extract_core_content 適用後のテキストを渡すこと
    (観光協会ページの関連スポット一覧・共通ナビ/フッターは既に除外されている)。"""

    if url in STRUCTURED_HOURS_CLOSED_EXCLUDED_URLS:
        hours = None
        closed = None
    else:
        hours = extract_labeled_field(text, "営業時間")
        closed = extract_labeled_field(text, "定休日")

    return {
        "hours": hours,
        "closed": closed,
        "menu": extract_menu_items(text),
    }


def diff_structured_fields(prev_fields: dict | None, new_fields: dict | None) -> list[str]:
    """構造化データ同士を比較し、画面表示用の「何が変わったか」の行を作る。

    どちらかの抽出結果が丸ごと無い(=取得失敗直後や旧形式のデータで
    まだ構造化データが無い)場合や、個々のフィールドが片方でも None の
    場合は、そのフィールドについて変更を断定せずスキップする。
    """

    if not prev_fields or not new_fields:
        return []

    changes: list[str] = []

    prev_hours = prev_fields.get("hours")
    new_hours = new_fields.get("hours")
    if prev_hours is not None and new_hours is not None and prev_hours != new_hours:
        changes.append(f"営業時間：{prev_hours} → {new_hours}")

    prev_closed = prev_fields.get("closed")
    new_closed = new_fields.get("closed")
    if prev_closed is not None and new_closed is not None and prev_closed != new_closed:
        changes.append(f"定休日：{prev_closed} → {new_closed}")

    prev_menu = prev_fields.get("menu")
    new_menu = new_fields.get("menu")
    if prev_menu is not None and new_menu is not None:
        # 一時的なHTML欠落やページ構造の変化により、価格の抽出件数が
        # 大きく減っただけの場合、実際には削除されていない項目まで
        # 「メニュー：〜が削除されました」と大量に誤検知してしまう。
        # 半分以上が消えたように見える場合は、削除ではなく取得の乱れと
        # みなし、そのメニュー比較(追加・削除・価格変更のすべて)を
        # 今回に限りスキップする。
        menu_extraction_looks_unreliable = (
            len(prev_menu) > 0 and len(new_menu) < len(prev_menu) * 0.5
        )

        if not menu_extraction_looks_unreliable:
            for name in new_menu:
                if name not in prev_menu:
                    changes.append(f"メニュー：「{name}」が追加されました")
            for name in prev_menu:
                if name not in new_menu:
                    changes.append(f"メニュー：「{name}」が削除されました")
            for name in new_menu:
                if name in prev_menu and prev_menu[name] != new_menu[name]:
                    changes.append(f"価格：{name} {prev_menu[name]} → {new_menu[name]}")

    return changes


def diff_summary(old: str, new: str) -> str:
    if not old:
        return "監視を開始しました。"

    keywords = {
        "営業時間": ["営業時間", "営業", "開店", "閉店"],
        "定休日": ["定休日", "休業日", "休み"],
        "メニュー": ["メニュー", "そば", "蕎麦"],
        "価格・料金": ["価格", "料金", "円", "税込", "瓶ビール"],
    }

    changed_items = []

    for label, words in keywords.items():
        old_hits = [w for w in words if w in old]
        new_hits = [w for w in words if w in new]

        if old_hits != new_hits:
            changed_items.append(label)

    if changed_items:
        return "・".join(changed_items) + "に変更候補があります。"

    return "公式ページの掲載内容が更新されました。"



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
        shop_changes: list[str] = []

        for url in meta["urls"]:
            try:
                r = requests.get(
                    url,
                    headers=HEADERS,
                    timeout=25
                )

                r.raise_for_status()

                text = normalize_html(r.content)
                text = extract_core_content(text, url)

                if len(text) < 80:
                    raise ValueError("page text too short")

                current_hash = digest(text, url)
                fields = extract_structured_fields(text, url)

                prev = old_sources.get(url, {})

                new_sources[url] = {
                    "hash": current_hash,
                    "text": text,
                    "fields": fields,
                    "checked_at": now,
                }

                if (
                    state.get("initialized")
                    and prev.get("hash")
                    and prev["hash"] != current_hash
                ):
                    shop_changes.extend(
                        diff_structured_fields(prev.get("fields"), fields)
                    )
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
            best_update["changes"] = shop_changes
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
