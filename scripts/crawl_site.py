#!/usr/bin/env python3
"""crawl_site.py — サイトマップ駆動の汎用サイトクローラ（サイト非依存）。

`scripts/crawl_archive.py`（Retty専用）の規約（stdlibのみ・USER_AGENT明示・
リクエスト間sleep）を踏襲するが、特定サイト名・ドメインをコードにハードコードしない。
対象は `--target` で渡すJSON設定ファイル（`.data/site-targets/*.json`）で指定する。
将来別サイトを追加する場合は設定ファイルを1つ追加するだけでよい（コード変更不要）。

target設定は単一サイトマップ（`sitemap_url`, 文字列）と複数サイトマップ（`sitemap_urls`,
配列）のどちらかを指定する。WordPress等、コンテンツ種別ごとにサイトマップが分割されて
いるサイト（例: miyarail.co.jp の `wp-sitemap-posts-page-1.xml`/`-post-1.xml`/`-delay-1.xml`
など）は `sitemap_urls` で必要な分だけ列挙する。複数指定時は各サイトマップの<loc>を
出現順で結合・重複排除してから通常通り1つの out_dir にクロールする（サイトマップ
インデックス自体を自動展開する機能ではない — 対象の子サイトマップURLを明示的に列挙する
方式。ネストの深さやサイトごとの命名規則に依存せず動作をシンプルに保つため）。

結合後のURL一覧は `--max-count` で先頭から打ち切られる（暴走防止）ため、`sitemap_urls`
の列挙順は重要度の高い（件数の少ない）ものを先に、件数の多いもの（例: 通常のブログ
投稿一覧）を最後に置くこと。そうしないと打ち切りで重要なコンテンツ種別が丸ごと
欠落しうる（miyarail.jsonでの実例: `-delay-1`/`-situation-1`は各1件のみだが実質必須の
情報のため先頭、`-post-1`（数百件規模）は最後に配置）。

処理内容:
  1. target設定の sitemap_url（または sitemap_urls 各々）から対象ページURL一覧を取得
     し、結合・重複排除する（標準 <loc> パース）。
  2. 各URLを取得し、HTML→タイトル/本文をタグ除去して抽出する
     （<script>/<style>/<nav>/<header>/<footer> は除外）。
  3. out_dir 配下に以下を書き出す:
       sitemap.xml       - 標準フォーマット。<loc> は元サイトのURLではなく、この
                            ミラー内の content.json への絶対URL（GitHub Pages経由）。
                            本文を含まないため、ページ数が増えてもファイルサイズは
                            小さく保たれる。
       <urlパス>/content.json - {"url","title","text","updated_at"}（本文はここ）。
       <urlパス>/index.html   - 生HTMLミラー（透明性・デバッグ用）。`<meta name="robots"
                            content="noindex,nofollow">` を挿入して書き出す — このミラーは
                            RAG用の機械可読ソースであり検索エンジンの索引対象ではないため
                            （GitHub Pagesのプロジェクトサイトはドメインルートの robots.txt
                            しか解釈できずリポジトリ単位の robots.txt は無視されるため、
                            ページ単位の<meta>挿入のみが実際に有効な除外手段）。
  4. `.data/site-targets-state/<name>.json` にページ毎の本文sha256を記録し、
     前回クロールと比較して変化があった時だけ updated_at を更新する（サイト側の
     sitemapにlastmodが無いことが多いため、実際に内容が変わった最終日を自前で推定する）。

使い方:
  python3 scripts/crawl_site.py --target .data/site-targets/miyarail.json --sleep 1

Copyright (c) 2026 樽石デジタル技術研究所合同会社
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib import robotparser
from xml.sax.saxutils import escape

USER_AGENT = "EntreprenAIs-Archive-crawler/0.1 (+https://entreprenais.com/#contact)"

_LOC_RE = re.compile(r"<loc>\s*([^<]+?)\s*</loc>", re.I)
_NON_HTML_EXT_RE = re.compile(
    r"\.(pdf|jpe?g|png|gif|svg|webp|css|js|zip|xml|ico|mp4|doc[x]?|xls[x]?)$", re.I
)
_WS_RE = re.compile(r"\s+")


def http_get(url: str, timeout: int = 30) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        print(f"  [WARN] HTTP {e.code} for {url}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] fetch failed {url}: {e}", file=sys.stderr)
    return None


def parse_sitemap_locs(xml_text: str) -> list[str]:
    """標準サイトマップXML文字列から <loc> の中身を順序維持・重複排除で返す。"""
    seen: dict[str, None] = {}
    for m in _LOC_RE.finditer(xml_text):
        seen.setdefault(m.group(1), None)
    return list(seen)


def resolve_sitemap_urls(target: dict) -> list[str]:
    """target設定から対象サイトマップURLのリストを返す。

    複数形 `sitemap_urls`（配列）を優先し、無ければ単数形 `sitemap_url`（文字列、
    既存targetとの後方互換）を1件のリストとして扱う。"""
    urls = target.get("sitemap_urls")
    if urls:
        return list(urls)
    single = target.get("sitemap_url")
    return [single] if single else []


def merge_sitemap_locs(xml_texts: list[str]) -> list[str]:
    """複数サイトマップXML文字列それぞれの<loc>を、出現順維持・重複排除で1つに結合する。"""
    seen: dict[str, None] = {}
    for xml_text in xml_texts:
        for loc in parse_sitemap_locs(xml_text):
            seen.setdefault(loc, None)
    return list(seen)


def same_domain(url: str, domain: str) -> bool:
    m = re.match(r"^https?://([^/]+)", url)
    return bool(m) and m.group(1).lower() == domain.lower()


def url_to_relpath(url: str) -> str:
    """URLのパス部分をフラットなディレクトリ相対パスに変換する（ルートは"."）。"""
    m = re.match(r"^https?://[^/]+(/.*)?$", url)
    path = (m.group(1) or "/") if m else "/"
    path = path.split("?")[0].split("#")[0].strip("/")
    return path or "."


class _TextExtractor(HTMLParser):
    """<script>/<style>/<nav>/<header>/<footer> を除いたタイトル・本文を集める簡易パーサ。

    セマンティックなタグ名だけでなく、class/id に nav・menu・footer 等の
    汎用的な命名パターンが含まれる要素（`<div class="drawer-menu-wrapper">` の
    ようなdiv実装のグローバルメニュー等、セマンティックタグを使わないテーマで
    頻出する）もスキップする。特定サイトの実際のクラス名を知っている必要はなく、
    Web制作で広く使われる命名慣習（nav/menu/footer/header/breadcrumb/sidebar）
    への一般的なヒューリスティックであり、サイト非依存という設計方針を崩さない。
    """

    _SKIP_TAGS = {"script", "style", "nav", "header", "footer", "noscript"}
    _SKIP_CLASS_HINTS = ("nav", "menu", "drawer", "footer", "header", "breadcrumb", "sidebar")
    # HTMLでは閉じタグを持たない要素（stackに積むと閉じタグ不在でずれるため除外）
    _VOID_TAGS = {
        "br", "img", "meta", "link", "input", "hr", "area",
        "base", "col", "embed", "source", "track", "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._title_parts: list[str] = []
        self._body_parts: list[str] = []
        self._in_title = False
        self._skip_stack: list[bool] = []  # 各開いているタグがskip対象かどうか

    def _matches_skip_class(self, attrs) -> bool:  # noqa: ANN001
        attr_text = " ".join((v or "") for k, v in attrs if k in ("class", "id")).lower()
        return any(hint in attr_text for hint in self._SKIP_CLASS_HINTS)

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag == "title":
            self._in_title = True
        if tag in self._VOID_TAGS:
            return
        already_skipping = bool(self._skip_stack) and self._skip_stack[-1]
        skip_here = already_skipping or tag in self._SKIP_TAGS or self._matches_skip_class(attrs)
        self._skip_stack.append(skip_here)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in self._VOID_TAGS:
            return
        if self._skip_stack:
            self._skip_stack.pop()

    def handle_data(self, data: str) -> None:
        if self._skip_stack and self._skip_stack[-1]:
            return
        (self._title_parts if self._in_title else self._body_parts).append(data)

    @property
    def title(self) -> str:
        return _WS_RE.sub(" ", "".join(self._title_parts)).strip()

    @property
    def body(self) -> str:
        return _WS_RE.sub(" ", "".join(self._body_parts)).strip()


def extract_title_and_text(html_text: str) -> tuple[str, str]:
    parser = _TextExtractor()
    parser.feed(html_text)
    return parser.title, parser.body


_NOINDEX_META = '<meta name="robots" content="noindex,nofollow">'
_HEAD_OPEN_RE = re.compile(r"<head[^>]*>", re.I)
_ROBOTS_META_RE = re.compile(r'<meta\s[^>]*name=["\']robots["\']', re.I)


def inject_noindex_meta(html_text: str) -> str:
    """ミラーとして書き出す生HTMLに noindex,nofollow メタタグを挿入する。

    GitHub Pagesのプロジェクトサイト（<user>.github.io/<repo>/...）は robots.txt を
    サイト単位（ドメインルート）でしか解釈できず、リポジトリ配下に置いても無視される
    ため、robots.txtによる除外は機能しない。ページ単位の<meta>挿入のみが実際に有効。

    このミラーは検索エンジンの索引対象ではなく、RAG用の機械可読ソース（content.json /
    サイト内検索の対象）として存在するため、対象サイトの意図に関わらず一律で
    noindexを付与する（既にrobotsメタが存在する場合は二重挿入しない）。"""
    if _ROBOTS_META_RE.search(html_text):
        return html_text
    m = _HEAD_OPEN_RE.search(html_text)
    if m:
        return html_text[: m.end()] + _NOINDEX_META + html_text[m.end() :]
    # <head>が無い（壊れた/簡易なHTML）場合のフォールバック: 独自の<head>を先頭に追加する。
    return f"<head>{_NOINDEX_META}</head>" + html_text


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _state_path(name: str) -> str:
    return os.path.join(".data", "site-targets-state", f"{name}.json")


def load_state(name: str) -> dict:
    path = _state_path(name)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(name: str, state: dict) -> None:
    path = _state_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


def site_rel_from_out_dir(out_dir: str) -> str:
    """out_dir（例: "docs/miyarail.co.jp"）から GitHub Pages 上の相対パスを求める
    （Pagesは docs/ をサイトルートとして配信するため、先頭の "docs/" を取り除く）。"""
    parts = out_dir.replace(os.sep, "/").strip("/").split("/")
    if not parts or parts[0] != "docs":
        raise ValueError(f"out_dir は docs/ 配下である必要があります: {out_dir}")
    return "/".join(parts[1:])


def write_sitemap(out_dir: str, content_urls: list[str]) -> None:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for u in content_urls:
        lines.append(f"  <url><loc>{escape(u)}</loc></url>")
    lines.append("</urlset>")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def crawl_target(
    target: dict, sleep: float, max_count: int, pages_base_url: str
) -> dict:
    name = target["name"]
    domain = target["domain"]
    sitemap_urls = resolve_sitemap_urls(target)
    out_dir = target["out_dir"]
    site_rel = site_rel_from_out_dir(out_dir)

    rp = robotparser.RobotFileParser()
    rp.set_url(f"https://{domain}/robots.txt")
    try:
        rp.read()
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] robots.txt取得失敗（許可扱いで続行）: {e}", file=sys.stderr)

    print(f"[crawl_site] target={name} sitemaps={sitemap_urls}")
    sitemap_xmls = [xml for xml in (http_get(u) for u in sitemap_urls) if xml]
    if not sitemap_xmls:
        raise RuntimeError(f"サイトマップの取得に全て失敗しました: {sitemap_urls}")

    urls = merge_sitemap_locs(sitemap_xmls)
    urls = [u for u in urls if same_domain(u, domain) and not _NON_HTML_EXT_RE.search(u)]
    urls = urls[:max_count]
    print(f"[crawl_site] {len(urls)} 件のURLを対象とする")

    state = load_state(name)
    now_iso = datetime.now(timezone.utc).isoformat()

    content_urls: list[str] = []
    ok = fail = skipped_robots = 0
    for i, url in enumerate(urls, 1):
        if not rp.can_fetch(USER_AGENT, url):
            print(f"  [{i}/{len(urls)}] robots.txtにより除外: {url}")
            skipped_robots += 1
            continue

        print(f"  [{i}/{len(urls)}] {url}")
        html_text = http_get(url)
        if not html_text:
            fail += 1
            continue

        title, text = extract_title_and_text(html_text)
        digest = sha256_hex(text)
        prev = state.get(url)
        updated_at = prev["updated_at"] if prev and prev.get("sha256") == digest else now_iso
        state[url] = {"sha256": digest, "updated_at": updated_at, "last_crawled": now_iso}

        relpath = url_to_relpath(url)
        page_dir = out_dir if relpath == "." else os.path.join(out_dir, relpath)
        os.makedirs(page_dir, exist_ok=True)
        with open(os.path.join(page_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write(inject_noindex_meta(html_text))
        content = {"url": url, "title": title, "text": text, "updated_at": updated_at}
        with open(os.path.join(page_dir, "content.json"), "w", encoding="utf-8") as f:
            json.dump(content, f, ensure_ascii=False, indent=2)

        content_relpath = "content.json" if relpath == "." else f"{relpath}/content.json"
        content_urls.append(f"{pages_base_url.rstrip('/')}/{site_rel}/{content_relpath}")
        ok += 1
        time.sleep(sleep)

    save_state(name, state)
    write_sitemap(out_dir, content_urls)
    return {"ok": ok, "fail": fail, "skipped_robots": skipped_robots, "total": len(urls)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="サイトマップ駆動の汎用サイトクローラ（サイト非依存・target設定JSON方式）"
    )
    ap.add_argument("--target", required=True, help="ターゲット設定JSONのパス（.data/site-targets/*.json）")
    ap.add_argument("--sleep", type=float, default=1.0, help="リクエスト間sleep秒")
    ap.add_argument("--max-count", type=int, default=200, help="最大処理ページ数（暴走防止）")
    ap.add_argument(
        "--pages-base-url",
        default="https://taru0216.github.io/archive-entreprenais-com",
        help="このリポジトリのGitHub Pages公開URL（sitemap.xmlの<loc>組み立てに使う）",
    )
    args = ap.parse_args(argv)

    with open(args.target, encoding="utf-8") as f:
        target = json.load(f)
    missing = [k for k in ("name", "domain", "out_dir") if k not in target]
    if missing:
        print(f"[ERROR] target設定に必須キーがありません {missing}: {args.target}", file=sys.stderr)
        return 1
    if not resolve_sitemap_urls(target):
        print(
            f"[ERROR] target設定に sitemap_url または sitemap_urls が必要です: {args.target}",
            file=sys.stderr,
        )
        return 1

    stats = crawl_target(
        target, sleep=args.sleep, max_count=args.max_count, pages_base_url=args.pages_base_url
    )
    print(
        f"[crawl_site] DONE {target['name']}: ok={stats['ok']} fail={stats['fail']} "
        f"skipped_robots={stats['skipped_robots']} total={stats['total']}"
    )

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"pages_saved={stats['ok']}\n")
            f.write(f"pages_failed={stats['fail']}\n")
    return 0 if stats["ok"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
