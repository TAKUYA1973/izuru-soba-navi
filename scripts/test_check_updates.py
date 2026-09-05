"""check_updates.py の自動更新チェックに対するテスト。

- 観光協会ページの「同じカテゴリーのスポット」等、店舗と無関係な変動部分を
  比較対象から除外できているか（誤検知の再発防止）
- 文字コード判定が誤っているページ（isiyama.com 相当）でも文字化けしないか
- 8店舗すべてが例外なく処理でき、無関係な変化では「更新あり」にならず、
  店舗固有の情報（営業時間など）が変わったときだけ検知できるか

を検証する。ネットワークへは一切アクセスせず、requests.get をモックして完結する。
"""

from __future__ import annotations

import importlib
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_updates  # noqa: E402


# --- 実際にキャッシュされていた観光協会ページのテキスト構造を模した fixture ---
# (data/source-state.json で確認した実際の並び: 共通ナビ → 店舗名/カテゴリ →
#  紹介文 → 所在地/TEL/営業時間/定休日/... → 「同じカテゴリーのスポット」 → 共通フッター)

KANKOU_NAV = (
    "いづるや – 栃木市観光協会 いづるや – 栃木市観光協会 Tochigi City Tourist Association "
    "/ 栃木市の公式観光情報サイト。旬なイベント・特集・観光プラン・開花情報や、定番のスポット・"
    "グルメ・お土産など様々な情報を発信 栃木市観光協会 スポット 観光情報 アクセス 検索 メニュー "
    "目的別に観光情報を探す 観光スポット グルメ 体験 おみやげ・買物 宿泊 メニューを閉じる "
    "栃木市ナビ 栃木市って？ 栃木市の歴史 運営情報 サイトマップ メニューを閉じる スポット トップページ"
)

KANKOU_FOOTER = (
    "栃木市観光協会 Tochigi City Tourist Association 個人情報保護方針 サイトポリシー "
    "特定商取引法に基づく表記 旅行業約款（PDF/572KB） 募集型旅行条件書（PDF/298KB） "
    "お問い合わせ (c) Tochigi City Tourist Association all rights reserved. TOP"
)


def make_kankou_html(hours: str, related_spots: str) -> bytes:
    body = (
        f"{KANKOU_NAV} グルメ いづるや いづるや いづるや 北エリア グルメ そば・うどん 出流そば "
        f"創業50年の元祖手打ち蕎麦。 所在地 栃木県栃木市出流町141 TEL 0282-31-0638 "
        f"営業時間 {hours} 定休日 毎週水曜日 メニュー 盛りそば 天ぷらそば 価格 800円〜 "
        f"専用駐車場 30台 公式WEB 公式ホームページ "
        f"21 同じカテゴリーのスポット {related_spots} {KANKOU_FOOTER}"
    )
    html = f"<html><head><title>t</title></head><body><p>{body}</p></body></html>"
    return html.encode("utf-8")


class FakeResponse:
    def __init__(self, content: bytes, status: int = 200):
        self.content = content
        self.text = content.decode("utf-8", errors="replace")
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise check_updates.requests.HTTPError(f"status {self.status_code}")


class NormalizeHtmlEncodingTest(unittest.TestCase):
    def test_decodes_utf8_body_without_charset_header(self):
        # isiyama.com のように Content-Type に charset が無い/誤っている場合、
        # requests の r.text は ISO-8859-1 に誤判定して文字化けする。
        # normalize_html には r.content (bytes) を渡し、BeautifulSoup 自身に
        # 文字コードを判定させることで、文字化けせず正しく読めることを確認する。
        html = "<html><body><p>そば処 いしやま 営業時間 10:30〜14:30</p></body></html>"
        raw_bytes = html.encode("utf-8")

        text = check_updates.normalize_html(raw_bytes)

        self.assertIn("そば処 いしやま", text)
        self.assertIn("営業時間", text)
        self.assertNotIn("ã", text)

    def test_decodes_shift_jis_body_with_meta_charset(self):
        html = (
            '<html><head><meta charset="shift_jis"></head>'
            "<body><p>そば処 いしやま 定休日 木曜日</p></body></html>"
        )
        raw_bytes = html.encode("shift_jis")

        text = check_updates.normalize_html(raw_bytes)

        self.assertIn("そば処 いしやま", text)
        self.assertIn("定休日", text)


class ExtractCoreContentTest(unittest.TestCase):
    def test_strips_related_spots_widget_on_kankou_pages(self):
        text_a = check_updates.normalize_html(
            make_kankou_html("11:00 ～ 18:00", "いしやま 福松 くろみや さとや")
        )
        text_b = check_updates.normalize_html(
            make_kankou_html("11:00 ～ 18:00", "岩本屋 新喜庵 稲安食道 好古壱番館")
        )

        url = "https://www.tochigi-kankou.or.jp/spot/izuruya"
        core_a = check_updates.extract_core_content(text_a, url)
        core_b = check_updates.extract_core_content(text_b, url)

        # 「同じカテゴリーのスポット」のローテーションだけが違う場合、
        # 抽出後のコア部分は完全に一致し、ハッシュも一致しなければならない。
        self.assertEqual(core_a, core_b)
        self.assertEqual(
            check_updates.digest(core_a, url), check_updates.digest(core_b, url)
        )

        # 関連スポットの店名そのものはコア抽出後には含まれない。
        self.assertNotIn("いしやま", core_a)
        self.assertNotIn("岩本屋", core_b)
        # 店舗固有の情報(営業時間・定休日・メニュー・価格)は削られずに保持されている。
        self.assertIn("営業時間", core_a)
        self.assertIn("11:00", core_a)
        self.assertIn("定休日", core_a)
        self.assertIn("毎週水曜日", core_a)
        self.assertIn("メニュー", core_a)
        self.assertIn("盛りそば", core_a)
        self.assertIn("価格", core_a)
        self.assertIn("800円", core_a)

    def test_detects_real_change_in_shop_specific_info(self):
        url = "https://www.tochigi-kankou.or.jp/spot/izuruya"
        text_before = check_updates.normalize_html(
            make_kankou_html("11:00 ～ 18:00", "いしやま 福松")
        )
        text_after = check_updates.normalize_html(
            make_kankou_html("11:00 ～ 19:00", "岩本屋 新喜庵")  # 営業時間が変わった
        )

        core_before = check_updates.extract_core_content(text_before, url)
        core_after = check_updates.extract_core_content(text_after, url)

        self.assertNotEqual(core_before, core_after)
        self.assertNotEqual(
            check_updates.digest(core_before, url), check_updates.digest(core_after, url)
        )

    def test_tag_order_shuffle_does_not_change_hash_on_kankou_pages(self):
        # 実データで確認された事象: カテゴリータグ（例:「出流そば」等のバッジ）の
        # 表示順が読み込みごとに入れ替わることがある。店舗情報自体は同じでも
        # 語順だけが変わるケースでハッシュが変わらないことを確認する。
        # (canonical_form は観光協会ページに限定して適用される)
        url = "https://www.tochigi-kankou.or.jp/spot/satoya"
        text_a = (
            "グルメ さとや さとや さとや 北エリア グルメ そば・うどん ランチ "
            "とちぎ小江戸ブランド 栃木IC 出流・星野 出流そば 所在地 栃木県栃木市出流町179 "
            "営業時間 11:00 ～ 16:00"
        )
        text_b = (
            "グルメ さとや さとや さとや 北エリア グルメ そば・うどん 出流そば "
            "ランチ とちぎ小江戸ブランド 栃木IC 出流・星野 所在地 栃木県栃木市出流町179 "
            "営業時間 11:00 ～ 16:00"
        )

        self.assertNotEqual(text_a, text_b)
        self.assertEqual(
            check_updates.digest(text_a, url), check_updates.digest(text_b, url)
        )

    def test_word_reordering_on_non_kankou_site_is_not_masked(self):
        # canonical_form を観光協会ページ以外にまで適用すると、単語の「対応関係」が
        # 失われ、価格やメニュー名が入れ替わっただけの意味のある変更を見逃してしまう
        # (例: もりそばとざるそばの値段が入れ替わったのに、同じ単語集合のため
        # 「変更なし」とみなされてしまう)。店舗自身のサイトでは単語順の違いが
        # そのままハッシュの違いとして検知されなければならない。
        url = "https://iduruya.co.jp/menu/"
        before = "メニュー もりそば 700円 ざるそば 800円 天ぷらそば 1200円"
        after = "メニュー もりそば 800円 ざるそば 700円 天ぷらそば 1200円"

        self.assertNotEqual(
            check_updates.digest(before, url), check_updates.digest(after, url)
        )

    def test_non_kankou_url_is_returned_unchanged(self):
        text = "元祖手打そば いづるや 同じカテゴリーのスポット (これは店の紹介文の一部)"
        result = check_updates.extract_core_content(text, "https://iduruya.co.jp/")
        self.assertEqual(result, text)


class ExtractLabeledFieldTest(unittest.TestCase):
    """営業時間・定休日・メニュー/価格の抽出関数を、実際にキャッシュされていた
    公式ページのテキスト構造に基づいて検証する。"""

    def test_extracts_hours_and_closed_from_shop_owned_site(self):
        # data/source-state.json で確認した iduruya.co.jp の実際の並び
        text = (
            "住所 〒328-0206 栃木県栃木市出流町141 TEL 0282-31-0638（代表） "
            "FAX 0282-31-1280 E-MAIL iduruya@cc9.ne.jp "
            "営業時間 11:00 ~ 17:00最終受付 定休日 毎週水曜日/毎月第3火曜日/元日 "
            "Instagram Facebook Contact Copyright"
        )
        self.assertEqual(
            check_updates.extract_labeled_field(text, "営業時間"),
            "11:00 ~ 17:00最終受付",
        )
        self.assertEqual(
            check_updates.extract_labeled_field(text, "定休日"),
            "毎週水曜日/毎月第3火曜日/元日",
        )

    def test_extracts_hours_and_closed_from_kankou_page_and_strips_trailing_spot_id(self):
        # data/source-state.json で確認した tochigi-kankou.or.jp/spot/iwamotoya の実際の並び
        text = (
            "所在地 栃木県栃木市出流町248 TEL 090-4595-2949 "
            "営業時間 11:00 ～ 17:00 定休日 金曜日 21"
        )
        self.assertEqual(
            check_updates.extract_labeled_field(text, "営業時間"), "11:00 ~ 17:00"
        )
        # 末尾の内部スポットID "21" が定休日の値に混入しない
        self.assertEqual(
            check_updates.extract_labeled_field(text, "定休日"), "金曜日"
        )

    def test_extracts_with_fullwidth_colon_label_separator(self):
        # data/source-state.json で確認した iduru-satoya.com の実際の並び
        text = "そば処 さとや 住所 ：栃木市出流町179 電話 ：0282(31)0919 営業時間 ：11：00～ 定休日 ：火曜日 （その他お休みになる場合があります） 大型バス駐車場ございます"
        self.assertEqual(check_updates.extract_labeled_field(text, "営業時間"), "11:00~")
        self.assertEqual(
            check_updates.extract_labeled_field(text, "定休日"),
            "火曜日 (その他お休みになる場合があります)",
        )

    def test_returns_none_when_label_not_present(self):
        text = "そば切り いしやま TEL 055-263-5381 メニュー お知らせ お問い合わせ"
        self.assertIsNone(check_updates.extract_labeled_field(text, "営業時間"))
        self.assertIsNone(check_updates.extract_labeled_field(text, "定休日"))

    def test_whitespace_and_width_variants_alone_do_not_change_extracted_value(self):
        # 表記揺れ（全角/半角コロン、改行、余分な空白）だけの違いは
        # 正規化により同一の値になる。
        text_a = "営業時間 11:00～18:00 定休日 火曜日"
        text_b = "営業時間　１１：００〜１８：００\n定休日　火曜日"
        self.assertEqual(
            check_updates.extract_labeled_field(text_a, "営業時間"),
            check_updates.extract_labeled_field(text_b, "営業時間"),
        )

    def test_extracts_menu_items_from_real_menu_page_structure(self):
        # data/source-state.json で確認した iduruya.co.jp/menu/ の実際の並び(抜粋)
        text = (
            "お 品 書 き 地元産の玄そばを使用しています。 おそば おそばのお供 "
            "いづるや名物のおそばです。 名代盆ざるそば 五合盛 2〜3人前 2,500円 "
            "八合盛 3〜4人前 4,000円 冷たいおそば もりそば 780円 ざるそば 880円 "
            "各種そば大盛り +250円"
        )
        menu = check_updates.extract_menu_items(text)
        self.assertIsNotNone(menu)
        # "〜"は表記揺れ正規化により"~"に統一される
        self.assertEqual(menu["八合盛 3~4人前"], "4,000円")
        self.assertEqual(menu["ざるそば"], "880円")
        self.assertEqual(menu["各種そば大盛り"], "+250円")

    def test_menu_returns_none_when_no_menu_heading_present(self):
        # メニュー/お品書きの見出し語が無いページでは、価格らしき数字があっても
        # 抽出を試みない(値を確実に抽出できない場合は変更と断定しない)
        text = "四季の郷 そば処さとや お知らせ ・2026年3月2日 3月13日まで休業"
        self.assertIsNone(check_updates.extract_menu_items(text))

    def test_menu_returns_empty_dict_when_heading_present_but_no_prices(self):
        text = "メニュー お知らせ お問い合わせ アクセス ブログ"
        self.assertEqual(check_updates.extract_menu_items(text), {})


class DiffStructuredFieldsTest(unittest.TestCase):
    """data/update-status.json に保存する「何が変わったか」の生成ロジックを検証する。
    ユーザー指定の11個の回帰テストに対応する。"""

    def test_detects_hours_change(self):
        prev = {"hours": "11:00〜17:00", "closed": "水曜日", "menu": None}
        new = {"hours": "11:00〜16:30", "closed": "水曜日", "menu": None}
        changes = check_updates.diff_structured_fields(prev, new)
        self.assertEqual(changes, ["営業時間：11:00〜17:00 → 11:00〜16:30"])

    def test_detects_closed_days_change(self):
        prev = {"hours": "11:00〜17:00", "closed": "水曜日", "menu": None}
        new = {"hours": "11:00〜17:00", "closed": "水曜日・木曜日", "menu": None}
        changes = check_updates.diff_structured_fields(prev, new)
        self.assertEqual(changes, ["定休日：水曜日 → 水曜日・木曜日"])

    def test_detects_menu_item_added(self):
        prev = {"hours": None, "closed": None, "menu": {"もりそば": "780円"}}
        new = {
            "hours": None,
            "closed": None,
            "menu": {"もりそば": "780円", "鴨南蛮そば": "1200円"},
        }
        changes = check_updates.diff_structured_fields(prev, new)
        self.assertEqual(changes, ["メニュー：「鴨南蛮そば」が追加されました"])

    def test_detects_menu_item_removed(self):
        prev = {
            "hours": None,
            "closed": None,
            "menu": {"もりそば": "780円", "鴨南蛮そば": "1200円"},
        }
        new = {"hours": None, "closed": None, "menu": {"もりそば": "780円"}}
        changes = check_updates.diff_structured_fields(prev, new)
        self.assertEqual(changes, ["メニュー：「鴨南蛮そば」が削除されました"])

    def test_detects_price_change(self):
        prev = {"hours": None, "closed": None, "menu": {"もりそば": "780円"}}
        new = {"hours": None, "closed": None, "menu": {"もりそば": "850円"}}
        changes = check_updates.diff_structured_fields(prev, new)
        self.assertEqual(changes, ["価格：もりそば 780円 → 850円"])

    def test_detects_multiple_simultaneous_changes(self):
        prev = {
            "hours": "11:00〜17:00",
            "closed": "水曜日",
            "menu": {"もりそば": "780円"},
        }
        new = {
            "hours": "11:00〜16:30",
            "closed": "水曜日・木曜日",
            "menu": {"もりそば": "850円"},
        }
        changes = check_updates.diff_structured_fields(prev, new)
        self.assertEqual(len(changes), 3)
        self.assertIn("営業時間：11:00〜17:00 → 11:00〜16:30", changes)
        self.assertIn("定休日：水曜日 → 水曜日・木曜日", changes)
        self.assertIn("価格：もりそば 780円 → 850円", changes)

    def test_whitespace_only_change_is_ignored(self):
        # extract_labeled_field の正規化により、両者は既に同じ値になっているはず
        # だが、diff_structured_fields 自体も同一値であれば変更を出さないことを
        # 直接確認する。
        prev = {"hours": "11:00〜17:00", "closed": "水曜日", "menu": None}
        new = {"hours": "11:00〜17:00", "closed": "水曜日", "menu": None}
        self.assertEqual(check_updates.diff_structured_fields(prev, new), [])

    def test_extraction_failure_on_either_side_is_not_treated_as_change(self):
        # 片方でも抽出できていない(None)フィールドは、変更と断定しない
        prev = {"hours": None, "closed": "水曜日", "menu": None}
        new = {"hours": "11:00〜17:00", "closed": "水曜日", "menu": None}
        self.assertEqual(check_updates.diff_structured_fields(prev, new), [])

    def test_missing_structured_data_on_either_side_yields_no_changes(self):
        # 旧形式のデータ(fieldsキーが無い)との比較や取得失敗直後は
        # 構造化データが丸ごと無い(None)ため、変更を断定しない。
        self.assertEqual(check_updates.diff_structured_fields(None, {"hours": "x"}), [])
        self.assertEqual(check_updates.diff_structured_fields({"hours": "x"}, None), [])

    def test_price_swap_between_two_items_is_detected(self):
        # 商品Aと商品Bの価格が入れ替わった場合、両方が「価格変更」として検知される
        # (canonical_form の単語集合比較のような、対応関係を失う比較では検知漏れになる)
        prev = {
            "hours": None,
            "closed": None,
            "menu": {"もりそば": "700円", "ざるそば": "800円"},
        }
        new = {
            "hours": None,
            "closed": None,
            "menu": {"もりそば": "800円", "ざるそば": "700円"},
        }
        changes = check_updates.diff_structured_fields(prev, new)
        self.assertEqual(len(changes), 2)
        self.assertIn("価格：もりそば 700円 → 800円", changes)
        self.assertIn("価格：ざるそば 800円 → 700円", changes)


class MainEndToEndTest(unittest.TestCase):
    """8店舗全ての URL をモックし、main() を通しで実行して検証する。"""

    def setUp(self):
        self.tmp_root = Path(
            __import__("tempfile").mkdtemp(prefix="izuru-soba-test-")
        )
        data_dir = self.tmp_root / "data"
        data_dir.mkdir()

        # 実際の shops.json と同じ8店舗・同じ urls 構成を使う
        real_cfg = json.loads(
            (Path(__file__).resolve().parents[1] / "data/shops.json").read_text(
                encoding="utf-8"
            )
        )
        (data_dir / "shops.json").write_text(
            json.dumps(real_cfg, ensure_ascii=False), encoding="utf-8"
        )

        self._patchers = [
            mock.patch.object(check_updates, "CFG", data_dir / "shops.json"),
            mock.patch.object(check_updates, "STATE", data_dir / "source-state.json"),
            mock.patch.object(check_updates, "STATUS", data_dir / "update-status.json"),
        ]
        for p in self._patchers:
            p.start()

        self.data_dir = data_dir
        self.cfg = real_cfg

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        import shutil

        shutil.rmtree(self.tmp_root, ignore_errors=True)

    def _all_urls(self):
        urls = set()
        for meta in self.cfg.values():
            urls.update(meta["urls"])
        return urls

    def _fake_get_factory(
        self,
        related_spots: str,
        hours_overrides=None,
        closed_overrides=None,
        menu_overrides=None,
        fail_urls=None,
    ):
        hours_overrides = hours_overrides or {}
        closed_overrides = closed_overrides or {}
        menu_overrides = menu_overrides or {}
        fail_urls = fail_urls or set()

        def fake_get(url, headers=None, timeout=None):
            if url in fail_urls:
                raise check_updates.requests.exceptions.ConnectTimeout(
                    f"simulated timeout for {url}"
                )

            if "tochigi-kankou.or.jp" in url:
                hours = hours_overrides.get(url, "11:00 ～ 18:00")
                return FakeResponse(make_kankou_html(hours, related_spots))

            # 店舗自身のサイト等、観光協会以外のページ用の簡易HTML
            # (先頭に「ダミー品」を挟み、直後の実際の商品名が直前語の
            #  混入なしにクリーンに抽出されるようにしている)
            hours = hours_overrides.get(url, "10:00〜18:00")
            closed = closed_overrides.get(url, "月曜日")
            menu = menu_overrides.get(url, "盛りそば 800円 天ぷらそば 1200円")
            body = (
                f"店舗情報ページ {url} 営業時間 {hours} 定休日 {closed} "
                f"メニュー ダミー品 100円 {menu} 所在地 栃木県栃木市出流町 "
                "電話番号 0282-00-0000 駐車場あり 家族連れ歓迎の手打ちそば店です。"
            )
            html = f"<html><body><p>{body}</p></body></html>".encode("utf-8")
            return FakeResponse(html)

        return fake_get

    def test_all_eight_shops_are_processed_without_error(self):
        self.assertEqual(len(self.cfg), 8, "shops.json の店舗数が8のままであることを前提")

        with mock.patch.object(
            check_updates.requests,
            "get",
            side_effect=self._fake_get_factory("いしやま 福松"),
        ):
            check_updates.main()

        state = json.loads(
            (self.data_dir / "source-state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(set(state["sources"].keys()), self._all_urls())

        # 初回実行でも、比較用の構造化データ(fields)自体はベースラインとして
        # 保存される。ただし「更新あり」にはならない(下のアサーションで確認)。
        kankou_izuruya = state["sources"]["https://www.tochigi-kankou.or.jp/spot/izuruya"]
        self.assertIn("fields", kankou_izuruya)
        self.assertEqual(kankou_izuruya["fields"]["hours"], "11:00 ~ 18:00")

        status = json.loads(
            (self.data_dir / "update-status.json").read_text(encoding="utf-8")
        )
        # 初回実行(initialized=False)では、比較対象がないため誰も「更新あり」にならない
        self.assertEqual(status["shops"], {})

    def test_related_spot_rotation_does_not_trigger_false_positive(self):
        # 1回目: ベースライン確立
        with mock.patch.object(
            check_updates.requests,
            "get",
            side_effect=self._fake_get_factory("いしやま 福松 くろみや"),
        ):
            check_updates.main()

        # 2回目: 店舗自身の情報は一切変えず、「同じカテゴリーのスポット」の
        # おすすめ店舗一覧だけがローテーションで変わったケースを再現
        with mock.patch.object(
            check_updates.requests,
            "get",
            side_effect=self._fake_get_factory("岩本屋 新喜庵 稲安食道 好古壱番館"),
        ):
            check_updates.main()

        status = json.loads(
            (self.data_dir / "update-status.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            status["shops"],
            {},
            "関連スポット一覧の入れ替わりだけで「更新あり」と誤検知している",
        )

    def test_fetch_failure_preserves_previous_fields_and_status(self):
        # 1回目: ベースライン確立(全URL成功)
        with mock.patch.object(
            check_updates.requests,
            "get",
            side_effect=self._fake_get_factory("いしやま 福松"),
        ):
            check_updates.main()

        state_after_first = json.loads(
            (self.data_dir / "source-state.json").read_text(encoding="utf-8")
        )
        izuruya_root = "https://iduruya.co.jp/"
        prev_fields = state_after_first["sources"][izuruya_root]["fields"]
        prev_hash = state_after_first["sources"][izuruya_root]["hash"]

        # 2回目: いづるやのURLの1つが一時的に取得失敗する
        with mock.patch.object(
            check_updates.requests,
            "get",
            side_effect=self._fake_get_factory(
                "岩本屋 新喜庵", fail_urls={izuruya_root}
            ),
        ):
            check_updates.main()

        state_after_failure = json.loads(
            (self.data_dir / "source-state.json").read_text(encoding="utf-8")
        )
        # 取得失敗時は、以前の正常値(hash/fields)がそのまま保持される
        self.assertEqual(
            state_after_failure["sources"][izuruya_root]["fields"], prev_fields
        )
        self.assertEqual(
            state_after_failure["sources"][izuruya_root]["hash"], prev_hash
        )

        status = json.loads(
            (self.data_dir / "update-status.json").read_text(encoding="utf-8")
        )
        # 一時的な取得失敗が「メニュー削除」等の変更として報告されない
        if "izuruya" in status["shops"]:
            for change in status["shops"]["izuruya"]["changes"]:
                self.assertNotIn("削除", change)

    def test_hours_and_price_change_detected_end_to_end(self):
        url = "https://iduruya.co.jp/"
        with mock.patch.object(
            check_updates.requests,
            "get",
            side_effect=self._fake_get_factory("いしやま 福松"),
        ):
            check_updates.main()

        with mock.patch.object(
            check_updates.requests,
            "get",
            side_effect=self._fake_get_factory(
                "岩本屋 新喜庵",
                hours_overrides={url: "11:00〜16:30"},
                menu_overrides={url: "盛りそば 850円 天ぷらそば 1200円"},
            ),
        ):
            check_updates.main()

        status = json.loads(
            (self.data_dir / "update-status.json").read_text(encoding="utf-8")
        )
        self.assertIn("izuruya", status["shops"])
        changes = status["shops"]["izuruya"]["changes"]
        self.assertTrue(
            any(c.startswith("営業時間：") for c in changes),
            changes,
        )
        self.assertTrue(
            any("もりそば" not in c and "盛りそば" in c and "→" in c for c in changes)
            or any("価格：盛りそば" in c for c in changes),
            changes,
        )

    def test_price_swap_between_two_products_detected_end_to_end(self):
        url = "https://iduruya.co.jp/"
        with mock.patch.object(
            check_updates.requests,
            "get",
            side_effect=self._fake_get_factory(
                "いしやま 福松",
                menu_overrides={url: "盛りそば 700円 ざるそば 800円"},
            ),
        ):
            check_updates.main()

        with mock.patch.object(
            check_updates.requests,
            "get",
            side_effect=self._fake_get_factory(
                "岩本屋 新喜庵",
                menu_overrides={url: "盛りそば 800円 ざるそば 700円"},
            ),
        ):
            check_updates.main()

        status = json.loads(
            (self.data_dir / "update-status.json").read_text(encoding="utf-8")
        )
        self.assertIn("izuruya", status["shops"])
        changes = status["shops"]["izuruya"]["changes"]
        self.assertIn("価格：盛りそば 700円 → 800円", changes)
        self.assertIn("価格：ざるそば 800円 → 700円", changes)

    def test_real_shop_change_is_still_detected(self):
        with mock.patch.object(
            check_updates.requests,
            "get",
            side_effect=self._fake_get_factory("いしやま 福松"),
        ):
            check_updates.main()

        izuruya_kankou_url = "https://www.tochigi-kankou.or.jp/spot/izuruya"
        with mock.patch.object(
            check_updates.requests,
            "get",
            side_effect=self._fake_get_factory(
                "岩本屋 新喜庵",
                hours_overrides={izuruya_kankou_url: "11:00 ～ 20:00"},
            ),
        ):
            check_updates.main()

        status = json.loads(
            (self.data_dir / "update-status.json").read_text(encoding="utf-8")
        )
        self.assertIn("izuruya", status["shops"])
        self.assertTrue(status["shops"]["izuruya"]["updated"])


if __name__ == "__main__":
    unittest.main()
