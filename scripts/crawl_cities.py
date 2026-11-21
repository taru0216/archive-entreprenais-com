#!/usr/bin/env python3
"""crawl_cities.py — 自治体公式サイトの農業・産業関連ページの生 HTML アーカイブ

自治体（cities）の農業・産業情報を Scrapy CitySpider で 1ステップクロールして保存する。
以前の 2ステップ構成（gen-csv + save-html）を統合し、中間 CSV を廃止した。
収集した cities データは marche ページの build 等、複数の用途に利用される。

使い方:
  # 1コマンドで完結（--target-domain で GHA matrix から1自治体ずつ渡す）
  python3 scripts/crawl_cities.py \\
    --target-csv .data/crawl-targets/marche-targets-chiba.csv \\
    --target-domain www.city.shiroi.chiba.jp \\
    --max-age-days 7

  # ドライラン（外部通信なし）
  python3 scripts/crawl_cities.py \\
    --target-csv .data/crawl-targets/marche-targets-chiba.csv \\
    --target-domain www.city.shiroi.chiba.jp \\
    --dry-run

CSV フォーマット（marche-targets.csv / marche-targets-chiba.csv 等）:
  ヘッダは city_url/city_slug/pref（旧形式）または slug/url/pref（新形式）の両方に対応。
  display_name 列は省略可能。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import time
from urllib.parse import urlparse

# crawl_archive モジュールから archive_path をインポート
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crawl_archive import archive_path  # noqa: E402

USER_AGENT = "EntreprenAIs-Archive-crawler/0.1 (+https://entreprenais.com/#contact)"

# 農業・マルシェ・イベント関連キーワード（URL パスまたはページテキストに含まれる場合に対象とする）
# 自治体サイトの農業関連 URL パターンを網羅的にカバーする
MARCHE_KEYWORDS = [
    # 日本語
    "農業", "農林", "産地", "直売", "マルシェ", "農産", "地産", "収穫", "栽培",
    "イベント", "祭", "産業祭", "収穫祭",
    # スコープ B 拡張（道の駅・特産品・ふるさと納税・旬）
    "道の駅", "特産", "名産", "ふるさと納税", "ふるさと", "旬", "観光", "物産",
    "市民農園", "体験農園", "グルメ", "食",
    # ローマ字 / 英語（自治体 URL で使われる主要パターン）
    "norin",       # 農林（農林水産 / 農林業）
    "nougyo",      # 農業（のうぎょう）
    "nogyo",       # 農業（短縮形）
    "nousuisan",   # 農水産（船橋市など）
    "chokubai",    # 直売
    "marche",      # マルシェ
    "event",       # イベント
    "agri",        # agriculture
    "farm",        # farm
    "sangyo",      # 産業（sangyo + 農業 URL に多い）
    "kanko",       # 観光（道の駅など）
    "kankou",      # 観光
    "chiisan",     # 地産地消
    "chisanch",    # 地産地消
    "michinoeki",  # 道の駅
    "michieki",    # 道の駅（短縮）
    "tokusan",     # 特産
    "meisan",      # 名産
    "furusato",    # ふるさと納税
    "nozei",       # 納税
    "bussan",      # 物産
    "kanko-bussan",
    "shun",        # 旬
    "gourmet",     # グルメ
]

# バイナリとみなす URL 拡張子（事前スキップ対象）
_BINARY_EXTENSIONS = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".gz", ".tar", ".rar", ".7z",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp",
    ".mp3", ".mp4", ".avi", ".mov", ".wmv", ".flv",
    ".csv", ".tsv",
    ".exe", ".dmg", ".pkg", ".deb", ".rpm",
)


def is_binary_url(url: str) -> bool:
    """URL のパス末尾がバイナリ拡張子かどうかを判定する。"""
    path = urlparse(url).path.lower()
    return path.endswith(_BINARY_EXTENSIONS)


def is_marche_url(url: str) -> bool:
    """URL パスが農業・マルシェ・イベント関連キーワードを含むか判定する。"""
    url_lower = url.lower()
    return any(kw.lower() in url_lower for kw in MARCHE_KEYWORDS)


def is_same_domain(url: str, base_domain: str) -> bool:
    """URL が base_domain と同一ドメインに属するか確認する。"""
    try:
        return urlparse(url).netloc == base_domain
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# .data/meta.json ヘルパー
# ---------------------------------------------------------------------------

def get_meta_path(url: str, out_dir: str = ".") -> str:
    """URL に対応する .data/meta.json のパスを返す。"""
    parsed = urlparse(url)
    fqdn = parsed.netloc
    url_path = parsed.path or "/"
    rel_path = archive_path(fqdn, url_path)
    page_dir = os.path.join(out_dir, os.path.dirname(rel_path))
    return os.path.join(page_dir, ".data", "meta.json")


def is_fresh(meta_path: str, max_age_days: float) -> bool:
    """meta.json が存在し last_crawled が max_age_days 以内なら True。"""
    if max_age_days <= 0:
        return False
    if not os.path.exists(meta_path):
        return False
    try:
        with open(meta_path) as f:
            meta = json.load(f)
        age_days = (time.time() - meta.get("last_crawled", 0)) / 86400
        return age_days < max_age_days
    except Exception:
        return False


def update_meta(url: str, content: bytes, out_dir: str = ".") -> None:
    """クロール後に .data/meta.json を更新する。"""
    meta_path = get_meta_path(url, out_dir)
    os.makedirs(os.path.dirname(meta_path), exist_ok=True)
    meta = {
        "url": url,
        "last_crawled": int(time.time()),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    with open(meta_path, "w") as f:
        json.dump(meta, f, separators=(",", ":"))


# ---------------------------------------------------------------------------
# CSV 読み込みヘルパー
# ---------------------------------------------------------------------------

def load_targets(
    csv_path: str,
    target_domain: str = "",
    shard_id: int = 0,
    num_shards: int = 1,
    row_start: int | None = None,
    row_end: int | None = None,
) -> list[tuple[str, str]]:
    """自治体リスト CSV を読み込み (city_slug, city_url) のリストを返す。

    CSV フォーマット互換性:
    - city_url/city_slug/pref（旧形式）
    - slug/url/pref/display_name（新形式: marche-targets-chiba.csv）

    Args:
        csv_path: CSV ファイルパス
        target_domain: FQDN でフィルタリング（空文字 = 全件）
        shard_id: シャード番号（0 始まり）。num_shards>1 時にレンジ分割する
        num_shards: 総シャード数（⌈√N⌉ 並列）。1 なら分割しない

    Returns:
        (city_slug, city_url) のリスト
    """
    targets: list[tuple[str, str]] = []
    all_rows: list[tuple[str, str]] = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # city_url または url（フォールバック）
            city_url = (row.get("city_url") or row.get("url") or "").strip()
            # city_slug または slug（フォールバック）
            city_slug = (row.get("city_slug") or row.get("slug") or "").strip()
            if not city_url or not city_slug:
                continue
            # --target-domain 指定時はそのドメインのみフィルタ
            if target_domain:
                row_fqdn = urlparse(city_url).hostname or ""
                if row_fqdn != target_domain:
                    continue
            all_rows.append((city_slug, city_url))

    # レンジ分割（⌈√N⌉ シャード並列用）。同一 CSV を全シャードが読み、自分の
    # レンジだけを処理する。row_start/row_end が明示された場合はそれを優先し
    # （shard_targets.py の start/end 出力と一致）、無ければ shard_id/num_shards
    # から ceil 分割で算出する。
    if row_start is not None and row_end is not None:
        targets = all_rows[row_start:row_end]
    elif num_shards > 1:
        per = -(-len(all_rows) // num_shards)  # ceil
        start = shard_id * per
        end = min(start + per, len(all_rows))
        targets = all_rows[start:end]
    else:
        targets = all_rows
    return targets


# ---------------------------------------------------------------------------
# Scrapy CitySpider（1ステップ版）
# ---------------------------------------------------------------------------

def run_city_spider(
    targets: list[tuple[str, str]],
    out_dir: str = ".",
    max_age_days: float = 7.0,
    depth_limit: int = 3,
    page_budget: int = 40,
    download_delay: float = 1.0,
    robots_obey: bool = True,
) -> tuple[int, int]:
    """Scrapy CitySpider を実行して HTML を保存する。

    Args:
        targets: (city_slug, city_url) のリスト
        out_dir: HTML 保存先ディレクトリ
        max_age_days: TTL（この日数より古い場合のみ再クロール）
        depth_limit: 再帰探索の深さ上限（トップ=0）
        page_budget: 自治体（ドメイン）あたりのページ数バジェット
        download_delay: リクエスト間遅延（秒・行儀）
        robots_obey: robots.txt を尊重するか（無人運転の既定 True）

    Returns:
        (saved, skipped) のタプル
    """
    try:
        import scrapy
        from scrapy.crawler import CrawlerProcess
        from scrapy.http import Response
    except ImportError:
        print("[ERROR] Scrapy not available. Run: pip install scrapy", file=sys.stderr)
        sys.exit(1)

    class CitySpider(scrapy.Spider):
        name = "city"
        custom_settings = {
            "CONCURRENT_REQUESTS": 4,
            "DOWNLOAD_DELAY": download_delay,
            "AUTOTHROTTLE_ENABLED": True,
            "AUTOTHROTTLE_START_DELAY": 1,
            "AUTOTHROTTLE_MAX_DELAY": 10,
            "AUTOTHROTTLE_TARGET_CONCURRENCY": 2.0,
            # robots.txt 尊重（無人運転の行儀: 既定 ON）。
            "ROBOTSTXT_OBEY": robots_obey,
            "DOWNLOADER_MIDDLEWARES": {
                "scrapy.downloadermiddlewares.offsite.OffsiteMiddleware": None,
            },
            "USER_AGENT": USER_AGENT,
            "LOG_LEVEL": "WARNING",
            "DOWNLOAD_TIMEOUT": 30,
            # 再帰探索の深さ上限（トップ=0。depth_limit=3 で 3 階層まで辿る）。
            "DEPTH_LIMIT": depth_limit,
        }

        def __init__(
            self,
            city_targets: list[tuple[str, str]],
            out_directory: str,
            max_age: float = 7.0,
            page_budget: int = 40,
            **kwargs,
        ):
            self.city_targets = city_targets
            self.out_directory = out_directory
            self.max_age = max_age
            # 自治体（ドメイン）あたりのページ数バジェット（無人運転の暴走防止）。
            self.page_budget = page_budget
            self._domain_pages: dict[str, int] = {}
            self.saved = 0
            self.skipped = 0
            super().__init__(**kwargs)

        def _budget_exceeded(self, url: str) -> bool:
            """対象ドメインのページ数がバジェットを超えたら True。"""
            dom = urlparse(url).netloc
            return self._domain_pages.get(dom, 0) >= self.page_budget

        def _count_page(self, url: str) -> None:
            dom = urlparse(url).netloc
            self._domain_pages[dom] = self._domain_pages.get(dom, 0) + 1

        def start_requests(self):
            # Scrapy 2.11 以前の互換性のため start_requests も実装する
            for city_slug, city_url in self.city_targets:
                meta_path = get_meta_path(city_url, self.out_directory)
                if is_fresh(meta_path, self.max_age):
                    self.skipped += 1
                    print(f"[skip] fresh: {city_url}", file=sys.stderr)
                    continue
                yield scrapy.Request(
                    city_url,
                    callback=self.parse_city,
                    errback=self.errback,
                    cb_kwargs={"city_slug": city_slug},
                )

        async def start(self):
            # Scrapy 2.11+ StartSpiderMiddleware 対応
            for city_slug, city_url in self.city_targets:
                meta_path = get_meta_path(city_url, self.out_directory)
                if is_fresh(meta_path, self.max_age):
                    self.skipped += 1
                    print(f"[skip] fresh: {city_url}", file=sys.stderr)
                    continue
                yield scrapy.Request(
                    city_url,
                    callback=self.parse_city,
                    errback=self.errback,
                    cb_kwargs={"city_slug": city_slug},
                )

        def parse_city(self, response: "Response", city_slug: str):
            """自治体トップページを保存し、関連リンクを再帰探索キューに追加する。"""
            self._save_page(response)
            yield from self._follow_links(response)

        def parse_page(self, response: "Response"):
            """子ページを保存し、さらに深く再帰探索する（depth_limit まで）。"""
            self._save_page(response)
            yield from self._follow_links(response)

        def _follow_links(self, response: "Response"):
            """同一ドメイン・関連語フィルタ・バジェット内のリンクを Request 化する。

            Scrapy の DEPTH_LIMIT が深さ上限を、page_budget が自治体あたりの
            ページ数上限を担保する（無人運転の二重バウンド）。
            """
            from urllib.parse import urljoin, urlparse as up
            base_domain = urlparse(response.url).netloc

            # ページ数バジェット超過ドメインはこれ以上辿らない
            if self._budget_exceeded(response.url):
                return

            for link in response.css("a::attr(href)").getall():
                abs_url = urljoin(response.url, link)
                parsed = up(abs_url)
                clean = parsed._replace(fragment="", query="").geturl()

                # 同一ドメイン以外（ドメイン許可リスト = シードドメインのみ）
                if parsed.netloc != base_domain:
                    continue
                if is_binary_url(clean):
                    continue
                # 関連語フィルタ（特産・農産・道の駅・旬・イベント・ふるさと納税）
                if not is_marche_url(clean):
                    continue
                meta_path = get_meta_path(clean, self.out_directory)
                if is_fresh(meta_path, self.max_age):
                    self.skipped += 1
                    continue

                yield scrapy.Request(
                    clean,
                    callback=self.parse_page,
                    errback=self.errback,
                )

        def _save_page(self, response: "Response") -> None:
            """HTML をアーカイブパスに保存し meta.json を更新する。"""
            url = response.url
            # ページ数バジェット超過なら保存もスキップ（暴走防止）
            if self._budget_exceeded(url):
                return
            parsed = urlparse(url)
            rel = archive_path(parsed.netloc, parsed.path or "/")
            dest = os.path.join(self.out_directory, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(response.text)
            update_meta(url, response.body, self.out_directory)
            self._count_page(url)
            self.saved += 1
            if self.saved % 10 == 0:
                print(f"  [INFO] saved {self.saved} pages", file=sys.stderr)

        def errback(self, failure):
            print(
                f"  [WARN] failed: {failure.request.url}: {failure.value}",
                file=sys.stderr,
            )

        def closed(self, reason):
            print(
                f"[INFO] CitySpider closed ({reason}): "
                f"saved={self.saved}, skipped={self.skipped}",
                file=sys.stderr,
            )

    process = CrawlerProcess()
    spider_inst = [None]

    def _on_spider_closed(spider, reason):
        spider_inst[0] = spider

    from scrapy import signals
    crawler = process.create_crawler(CitySpider)
    crawler.signals.connect(_on_spider_closed, signal=signals.spider_closed)
    process.crawl(
        crawler,
        city_targets=targets,
        out_directory=out_dir,
        max_age=max_age_days,
        page_budget=page_budget,
    )
    process.start()

    sp = spider_inst[0]
    if sp:
        return sp.saved, sp.skipped
    return 0, 0


# ---------------------------------------------------------------------------
# CLI エントリポイント
# ---------------------------------------------------------------------------

def main() -> None:
    """1ステップクロール CLI エントリポイント。"""
    ap = argparse.ArgumentParser(
        prog="crawl_cities.py",
        description="自治体公式サイトの農業・産業関連ページを Scrapy CitySpider で 1ステップクロール・保存する",
    )
    ap.add_argument(
        "--target-csv",
        required=True,
        help="自治体リスト CSV パス（ヘッダ city_url/city_slug/pref または slug/url/pref）",
    )
    ap.add_argument(
        "--target-domain",
        default="",
        help="処理対象の FQDN（指定した場合、この FQDN の自治体のみ処理する）",
    )
    ap.add_argument(
        "--max-age-days",
        type=float,
        default=7.0,
        help="この日数より古い .data/meta.json は再クロール対象（0=常に再クロール、デフォルト: 7）",
    )
    ap.add_argument(
        "--out-dir",
        default=".",
        help="HTML 保存先ディレクトリ（デフォルト: カレントディレクトリ）",
    )
    ap.add_argument("--shard-id", type=int, default=0, help="シャード番号（0 始まり）")
    ap.add_argument(
        "--num-shards", type=int, default=1, help="総シャード数（⌈√N⌉ 並列。1 で無分割）"
    )
    ap.add_argument(
        "--row-start", type=int, default=None,
        help="CSV 行レンジ開始（shard_targets.py の start 出力。指定時 num-shards より優先）",
    )
    ap.add_argument(
        "--row-end", type=int, default=None, help="CSV 行レンジ終了（exclusive）",
    )
    ap.add_argument(
        "--depth-limit", type=int, default=3, help="再帰探索の深さ上限（トップ=0、デフォルト: 3）"
    )
    ap.add_argument(
        "--page-budget",
        type=int,
        default=40,
        help="自治体（ドメイン）あたりのページ数バジェット（暴走防止、デフォルト: 40）",
    )
    ap.add_argument(
        "--download-delay", type=float, default=1.0, help="リクエスト間遅延（秒・行儀）"
    )
    ap.add_argument(
        "--no-robots",
        action="store_true",
        help="robots.txt を無視する（既定は尊重。無人運転では指定しないこと）",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="外部通信なしでターゲット解析のみ確認（テスト用）",
    )
    args = ap.parse_args()

    # 対象 CSV を読み込む
    if not os.path.exists(args.target_csv):
        print(f"[ERROR] --target-csv が見つかりません: {args.target_csv}", file=sys.stderr)
        sys.exit(1)

    targets = load_targets(
        args.target_csv,
        target_domain=args.target_domain,
        shard_id=args.shard_id,
        num_shards=args.num_shards,
        row_start=args.row_start,
        row_end=args.row_end,
    )

    if not targets:
        print(
            f"[WARN] 対象自治体が 0 件です（target_csv={args.target_csv}, "
            f"target_domain={args.target_domain or '(全件)'}）",
            file=sys.stderr,
        )
        sys.exit(0)

    print(
        f"[crawl_cities] {len(targets)} 自治体を処理します"
        f"{' (dry-run)' if args.dry_run else ''}",
        file=sys.stderr,
    )
    for slug, url in targets:
        print(f"  {slug}: {url}", file=sys.stderr)

    if args.dry_run:
        print("[crawl_cities] dry-run 完了 — 外部通信なし", file=sys.stderr)
        sys.exit(0)

    saved, skipped = run_city_spider(
        targets,
        out_dir=args.out_dir,
        max_age_days=args.max_age_days,
        depth_limit=args.depth_limit,
        page_budget=args.page_budget,
        download_delay=args.download_delay,
        robots_obey=not args.no_robots,
    )

    print(f"\n[crawl_cities] DONE: {saved} saved, {skipped} skipped")

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"pages_saved={saved}\n")
            f.write(f"pages_skipped={skipped}\n")

    sys.exit(0)


if __name__ == "__main__":
    main()
