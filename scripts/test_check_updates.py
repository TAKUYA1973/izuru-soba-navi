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
        f"営業時間 {hours} 定休日 毎週水曜日 専用駐車場 30台 公式WEB 公式ホームページ "
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
        self.assertEqual(check_updates.digest(core_a), check_updates.digest(core_b))

        # 関連スポットの店名そのものはコア抽出後には含まれない。
        self.assertNotIn("いしやま", core_a)
        self.assertNotIn("岩本屋", core_b)
        # 店舗固有の情報は保持されている。
        self.assertIn("営業時間", core_a)
        self.assertIn("11:00", core_a)

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
            check_updates.digest(core_before), check_updates.digest(core_after)
        )

    def test_tag_order_shuffle_does_not_change_hash(self):
        # 実データで確認された事象: カテゴリータグ（例:「出流そば」等のバッジ）の
        # 表示順が読み込みごとに入れ替わることがある。店舗情報自体は同じでも
        # 語順だけが変わるケースでハッシュが変わらないことを確認する。
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
        self.assertEqual(check_updates.digest(text_a), check_updates.digest(text_b))

    def test_non_kankou_url_is_returned_unchanged(self):
        text = "元祖手打そば いづるや 同じカテゴリーのスポット (これは店の紹介文の一部)"
        result = check_updates.extract_core_content(text, "https://iduruya.co.jp/")
        self.assertEqual(result, text)


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

    def _fake_get_factory(self, related_spots: str, hours_overrides=None):
        hours_overrides = hours_overrides or {}

        def fake_get(url, headers=None, timeout=None):
            if "tochigi-kankou.or.jp" in url:
                hours = hours_overrides.get(url, "11:00 ～ 18:00")
                return FakeResponse(make_kankou_html(hours, related_spots))
            # 店舗自身のサイト等、観光協会以外のページ用の簡易HTML
            body = (
                f"店舗情報ページ {url} 営業時間 10:00〜18:00 定休日 月曜日 "
                "メニュー 盛りそば 800円 天ぷらそば 1200円 所在地 栃木県栃木市出流町 "
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
