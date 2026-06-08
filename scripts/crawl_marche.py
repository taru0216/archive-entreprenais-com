#!/usr/bin/env python3
"""crawl_marche.py — 自治体公式サイト・道の駅サイトの生 HTML アーカイブ

2 ステップ構成:
  gen-csv  : 自治体トップページから農業・イベントページ URL を探索して CSV 出力
  save-html: CSV から各ページを archive_path() 規則で保存

使い方:
  python3 scripts/crawl_marche.py gen-csv \
    --target-csv .data/crawl-targets/marche-targets.csv \
    --out .data/crawl-targets/marche-pages.csv \
    --depth 1 --sleep 3

  python3 scripts/crawl_marche.py save-html \
    --csv .data/crawl-targets/marche-pages.csv \
    --sleep 3

  # ドライラン（外部通信なし・URL 生成のみ確認）
  python3 scripts/crawl_marche.py gen-csv \
    --target-csv .data/crawl-targets/marche-targets.csv \
    --out .data/crawl-targets/marche-pages.csv \
    --dry-run
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
import urllib.request
import urllib.error
import urllib.robotparser
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

# crawl_archive モジュールから archive_path をインポート
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crawl_archive import archive_path  # noqa: E402

USER_AGENT = "EntreprenAIs-Archive-crawler/0.1 (+https://entreprenais.com/#contact)"

# 農業・マルシェ・イベント関連キーワード（URL パスまたはページテキストに含まれる場合に対象とする）
MARCHE_KEYWORDS = [
    # 日本語
    "農業", "農林", "産地", "直売", "マルシェ", "農産", "地産", "収穫", "栽培",
    "イベント", "祭", "産業祭", "収穫祭",
    # ローマ字 / 英語
    "norin", "nougyo", "nogyo", "chokubai", "marche", "event", "agri", "farm",
]

# robots.txt キャッシュ
_robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}

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


def http_get(url: str, timeout: int = 30) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get_content_type() or ""
            # text/html 以外（PDF・画像・バイナリ等）はリンク探索対象外
            if not content_type.startswith("text/html"):
                return None
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        print(f"  [WARN] HTTP {e.code} for {url}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] fetch failed {url}: {e}", file=sys.stderr)
    return None


def is_allowed_by_robots(url: str) -> bool:
    """robots.txt の Disallow をチェックする。"""
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    if base not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(base + "/robots.txt")
        try:
            rp.read()
        except Exception:  # noqa: BLE001
            # robots.txt 取得失敗はアクセス許可とみなす
            pass
        _robots_cache[base] = rp
    return _robots_cache[base].can_fetch(USER_AGENT, url)


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


class LinkExtractor(HTMLParser):
    """HTML からリンク URL を抽出するパーサー。"""

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value:
                    abs_url = urljoin(self.base_url, value)
                    # フラグメント・クエリ除去
                    parsed = urlparse(abs_url)
                    clean = parsed._replace(fragment="", query="").geturl()
                    self.links.append(clean)


def discover_marche_urls(
    city_url: str,
    city_slug: str,
    max_depth: int = 1,
    sleep_sec: float = 1.0,
    dry_run: bool = False,
) -> list[str]:
    """自治体トップページから農業・マルシェページ URL を BFS 探索する。

    Args:
        city_url: 自治体トップページ URL
        city_slug: 自治体スラグ（ログ用）
        max_depth: 探索深さ上限（ドメイン内のみ）
        sleep_sec: リクエスト間 sleep 秒
        dry_run: True の場合、外部通信なしで空リストを返す

    Returns:
        農業・マルシェ関連ページの URL リスト（重複除去済み）
    """
    if dry_run:
        print(f"[gen-csv] dry-run: {city_slug} ({city_url}) — スキップ")
        return []

    base_domain = urlparse(city_url).netloc
    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(city_url, 0)]
    found: list[str] = []

    while queue:
        url, depth = queue.pop(0)

        # 正規化（末尾スラッシュ統一）
        if url in visited:
            continue
        visited.add(url)

        # robots.txt チェック
        if not is_allowed_by_robots(url):
            print(f"  [robots] disallowed: {url}")
            continue

        print(f"[discover] depth={depth} {url}")
        html = http_get(url)
        if not html:
            continue

        # 農業・マルシェキーワードチェック（URL に含まれる場合に収集）
        if depth > 0 and is_marche_url(url):
            if url not in found:
                found.append(url)
                print(f"  [found] {url}")

        # 深さ上限以内なら子リンクを追加
        if depth < max_depth:
            extractor = LinkExtractor(url)
            try:
                extractor.feed(html)
            except Exception as e:  # noqa: BLE001
                print(f"  [WARN] HTML parse error {url}: {e}", file=sys.stderr)
            for link in extractor.links:
                if link not in visited and is_same_domain(link, base_domain):
                    # バイナリ拡張子の URL はキューに追加しない
                    if is_binary_url(link):
                        print(f"  [SKIP] binary URL: {link}", file=sys.stderr)
                        continue
                    queue.append((link, depth + 1))

        time.sleep(sleep_sec)

    return found


def gen_csv_main(argv: list[str]) -> int:
    """Step① discovery: 自治体リスト → 農業・マルシェページ URL の CSV を生成する。"""
    ap = argparse.ArgumentParser(
        prog="crawl_marche.py gen-csv",
        description="Step① discovery: 自治体トップページから農業・マルシェページ URL を探索して CSV 出力",
    )
    ap.add_argument(
        "--target-csv",
        required=True,
        help="自治体リスト CSV パス（ヘッダ city_url,city_slug,pref）",
    )
    ap.add_argument(
        "--out",
        required=True,
        help="出力 CSV パス（ヘッダ page_url,fqdn,city_slug,pref）",
    )
    ap.add_argument("--depth", type=int, default=1, help="BFS 探索深さ上限（デフォルト: 1）")
    ap.add_argument("--sleep", type=float, default=1.0, help="リクエスト間 sleep 秒")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="外部通信なしで URL リスト生成のみ確認（テスト用）",
    )
    ap.add_argument(
        "--target-domain",
        default="",
        help="処理対象の FQDN（指定した場合、この FQDN の自治体のみ処理する）",
    )
    args = ap.parse_args(argv)

    # 自治体リスト CSV を読み込む
    targets: list[tuple[str, str, str]] = []
    with open(args.target_csv, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            city_url = (row.get("city_url") or "").strip()
            city_slug = (row.get("city_slug") or "").strip()
            pref = (row.get("pref") or "").strip()
            if city_url and city_slug:
                # --target-domain 指定時はそのドメインのみフィルタ
                if args.target_domain:
                    row_fqdn = urlparse(city_url).hostname or ""
                    if row_fqdn != args.target_domain:
                        continue
                targets.append((city_url, city_slug, pref))

    if not targets:
        print(f"[gen-csv] 自治体リストが空です: {args.target_csv}", file=sys.stderr)
        return 1

    print(f"[gen-csv] {len(targets)} 自治体を処理します")

    # 出力 CSV 準備
    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    total = 0
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["page_url", "fqdn", "city_slug", "pref"])

        for city_url, city_slug, pref in targets:
            fqdn = urlparse(city_url).netloc
            urls = discover_marche_urls(
                city_url,
                city_slug,
                max_depth=args.depth,
                sleep_sec=args.sleep,
                dry_run=args.dry_run,
            )
            for url in urls:
                row_fqdn = urlparse(url).netloc
                writer.writerow([url, row_fqdn, city_slug, pref])
                total += 1
            print(f"[gen-csv] {city_slug}: {len(urls)} ページ発見")
            _ = fqdn  # 未使用変数の警告抑制

    print(f"[gen-csv] 合計 {total} ページ → {args.out}")

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"pages_discovered={total}\n")

    return 0


def save_html_main(argv: list[str]) -> int:
    """Step② archive: CSV → 生 HTML を archive_path() 規則で保存する。"""
    ap = argparse.ArgumentParser(
        prog="crawl_marche.py save-html",
        description="Step② archive: CSV の URL リストから生 HTML を archive_path() 規則で保存",
    )
    ap.add_argument(
        "--csv",
        required=True,
        help="クロール対象 CSV パス（ヘッダ page_url,fqdn,city_slug,pref）",
    )
    ap.add_argument("--sleep", type=float, default=1.0, help="リクエスト間 sleep 秒")
    ap.add_argument("--max-count", type=int, default=0, help="最大処理件数（0 = 全件）")
    args = ap.parse_args(argv)

    # CSV 読み込み
    rows: list[dict] = []
    with open(args.csv, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            url = (row.get("page_url") or "").strip()
            if url:
                rows.append({"url": url})

    if args.max_count > 0:
        rows = rows[: args.max_count]

    print(f"[save-html] {len(rows)} ページを処理します")

    ok, fail = 0, 0
    for i, row in enumerate(rows, 1):
        url = row["url"]

        # robots.txt チェック
        if not is_allowed_by_robots(url):
            print(f"  [robots] disallowed: {url}")
            fail += 1
            continue

        print(f"[fetch {i}/{len(rows)}] {url}")
        html = http_get(url)
        if not html:
            fail += 1
            continue

        try:
            parsed = urlparse(url)
            fqdn = parsed.netloc
            url_path = parsed.path or "/"
            rel_path = archive_path(fqdn, url_path)
            os.makedirs(os.path.dirname(rel_path) if os.path.dirname(rel_path) else ".", exist_ok=True)
            with open(rel_path, "w", encoding="utf-8") as f:
                f.write(html)
            ok += 1
            print(f"  -> {rel_path}")
        except Exception as e:  # noqa: BLE001
            print(f"  [ERROR] save failed {url}: {e}", file=sys.stderr)
            fail += 1

        time.sleep(args.sleep)

    print(f"\n[save-html] DONE: {ok} saved, {fail} failed")

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"pages_saved={ok}\n")
            f.write(f"pages_failed={fail}\n")

    return 0 if ok > 0 else 1


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: crawl_marche.py <subcommand> [options]", file=sys.stderr)
        print("  gen-csv   Step① discovery: 自治体リスト → マルシェページ URL の CSV", file=sys.stderr)
        print("  save-html Step② archive: CSV → 生 HTML 保存", file=sys.stderr)
        return 1

    sub = sys.argv[1]
    if sub == "gen-csv":
        return gen_csv_main(sys.argv[2:])
    elif sub == "save-html":
        return save_html_main(sys.argv[2:])
    else:
        print(f"Unknown subcommand: {sub}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
