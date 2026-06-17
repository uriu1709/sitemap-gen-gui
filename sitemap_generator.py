#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XML サイトマップ生成ツール（Playwright版）
- JS描画後のDOMからリンク抽出（ヘッダー・フッター対応）
- リダイレクト検出・除外
- priority / changefreq / lastmod 設定
- 除外URLパターン設定
- 50,000件超の分割サイトマップ対応
- robots.txt Disallow/Crawl-delay 準拠
- X-Robots-Tag / meta noindex・nofollow 対応
- canonical URL 収集
- SSRF対策（プライベートIPブロック）
- 429/500/503 バックオフリトライ
- エラー率監視・自動中断
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import multiprocessing
import threading
from urllib.parse import urlparse, urlsplit, urlunsplit, quote, urljoin
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import urllib.request
import xml.etree.ElementTree as ET
from xml.dom import minidom
import re
import os
import sys
import queue
import time
import socket
import ipaddress


USER_AGENT = 'SitemapGenBot/1.0 (+https://github.com/uriu1709/sitemap-gen-gui)'


def normalize_url(url):
    """URL正規化: フラグメント(#)除去・クエリ保持。
    ルートパスは末尾スラッシュ(/)ありに統一し（Playwright の page.url と整合）、
    サブディレクトリの末尾スラッシュはサーバーの正規形を尊重して保持する
    （非正規URLの登録・無駄なリダイレクトを避けるため）。"""
    if not url:
        return url
    url = url.split('#')[0]
    # urlsplit を使い ;params（例: /path;sid=123）を path 内に保持する
    p = urlsplit(url)
    if not p.scheme or not p.netloc:
        return url
    path = p.path or '/'
    # scheme / netloc は RFC3986 上 大文字小文字を区別しないため小文字に統一
    scheme = p.scheme.lower()
    netloc = p.netloc.lower()
    # デフォルトポートは除去（Playwright の page.url と整合させ誤リダイレクト判定を防ぐ）
    if scheme == 'http' and netloc.endswith(':80'):
        netloc = netloc[:-3]
    elif scheme == 'https' and netloc.endswith(':443'):
        netloc = netloc[:-4]
    rebuilt = f'{scheme}://{netloc}{path}'
    if p.query:
        rebuilt += '?' + p.query
    return rebuilt


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """SSRF対策付きリダイレクトハンドラ。
    リダイレクト先が安全（_is_safe_url）な場合のみ追従し、http→https 等の
    正当なリダイレクトは許可しつつプライベートIP等への誘導を遮断する。"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _is_safe_url(newurl):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def parse_retry_after(value, default=30):
    """Retry-After ヘッダーを秒数として返す（秒数・HTTP-date 両対応）。"""
    if not value:
        return default
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        dt = parsedate_to_datetime(value)
        # naive な場合は UTC とみなし、必ず aware 同士で計算（実行環境のTZに依存しない）
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        return max(int(delta), 0)
    except Exception:
        return default


def _goto(page, url, timeout=30000, settle_timeout=5000):
    """domcontentloaded で遷移後、JS描画完了を待つ（networkidle が来なくても続行）。"""
    resp = page.goto(url, timeout=timeout, wait_until='domcontentloaded')
    try:
        page.wait_for_load_state('networkidle', timeout=settle_timeout)
    except Exception:
        pass
    return resp


def _get_browsers_path():
    """PyInstaller exe実行時はexeと同階層の 'browsers' フォルダを参照"""
    if getattr(sys, 'frozen', False):
        return os.path.join(os.path.dirname(sys.executable), 'browsers')
    return None


def _is_private_host(hostname):
    """ホスト名がプライベート/ループバック/リンクローカルIPに解決される場合True"""
    try:
        infos = socket.getaddrinfo(hostname, None)
        for info in infos:
            ip_str = info[4][0]
            ip = ipaddress.ip_address(ip_str)
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast
                    or ip.is_unspecified):
                return True
        return False
    except Exception:
        return True  # DNS解決失敗はブロック扱い


def _is_safe_url(url):
    """スキームがhttp/httpsで、プライベートIPでないことを確認"""
    try:
        p = urlparse(url)
        if p.scheme not in ('http', 'https'):
            return False
        if not p.hostname:
            return False
        if _is_private_host(p.hostname):
            return False
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────
# クローラー（別プロセスで実行）
# ─────────────────────────────────────────────
def crawler_process(base_url, max_pages, delay, exclude_patterns, pipe_conn, stop_flag):
    # PyInstaller exe時はブラウザパスを設定
    browsers_path = _get_browsers_path()
    if browsers_path:
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = browsers_path

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pipe_conn.send(('ERROR', 'Playwrightがインストールされていません。\npip install playwright\nplaywright install chromium'))
        pipe_conn.close()
        return

    # 起点URLの安全チェック
    if not _is_safe_url(base_url):
        pipe_conn.send(('ERROR', f'安全でないURLです（プライベートIPまたは不正スキーム）: {base_url}'))
        pipe_conn.close()
        return

    # normalize_url 経由で抽出し、リンク側の正規化（デフォルトポート除去等）と整合させる
    domain = urlparse(normalize_url(base_url)).netloc.lower()
    url_data = {}
    to_visit = [normalize_url(base_url)]
    visited = set()
    fetched = 0   # 実際に取得を試みたページ数（= 最大ページ数の基準）

    # エラー率監視用
    recent_results = []   # True=成功, False=失敗
    ERROR_RATE_WINDOW = 20
    ERROR_RATE_THRESHOLD = 0.7

    def is_excluded(url):
        for pat in exclude_patterns:
            pat = pat.strip()
            if not pat:
                continue
            try:
                if re.search(pat, url):
                    return True
            except re.error:
                pass
        return False

    def record_result(success):
        recent_results.append(success)
        if len(recent_results) > ERROR_RATE_WINDOW:
            recent_results.pop(0)

    def high_error_rate():
        if len(recent_results) < ERROR_RATE_WINDOW:
            return False
        fail_rate = recent_results.count(False) / len(recent_results)
        return fail_rate >= ERROR_RATE_THRESHOLD

    # robots.txt を取得して Disallow / Crawl-delay を確認
    from urllib.robotparser import RobotFileParser
    crawl_delay = delay
    rp = RobotFileParser()
    try:
        parsed_base = urlparse(base_url)
        robots_url = f'{parsed_base.scheme}://{parsed_base.netloc}/robots.txt'
        rp.set_url(robots_url)
        # タイムアウト付きで取得（標準の rp.read() はタイムアウトが無くハングし得る）。
        # リダイレクトは安全なURLのみ追従（SSRF対策）。
        if not _is_safe_url(robots_url):
            raise ValueError('unsafe robots url')
        opener = urllib.request.build_opener(_SafeRedirectHandler)
        req = urllib.request.Request(robots_url, headers={'User-Agent': USER_AGENT})
        with opener.open(req, timeout=10) as r:
            # リダイレクト拒否時は 3xx がそのまま返るため、200 以外は robots 無しとして扱う
            if r.getcode() != 200:
                raise ValueError(f'invalid status: {r.getcode()}')
            text = r.read().decode('utf-8', errors='ignore')
        rp.parse(text.splitlines())
        robots_delay = rp.crawl_delay('*')
        if robots_delay:
            crawl_delay = max(delay, float(robots_delay))
            pipe_conn.send(('LOG', f'  robots.txt Crawl-delay: {robots_delay}s → {crawl_delay}s で動作'))
    except Exception:
        rp = None
        pipe_conn.send(('LOG', '  robots.txt 取得をスキップ（未設置/タイムアウト等）'))

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=USER_AGENT,
                service_workers='block',
            )
            page = context.new_page()

            # ダイアログ（alert/confirm/prompt）を自動で閉じる
            page.on('dialog', lambda dialog: dialog.dismiss())

            # 帯域節約のため画像・メディア・フォント・CSSのみブロック。
            # fetch/xhr/websocket 等は JS描画に必要なため許可する。
            BLOCK_TYPES = {'image', 'media', 'font', 'stylesheet'}

            def handle_route(route):
                try:
                    p = urlparse(route.request.url)
                    if p.scheme not in ('http', 'https'):
                        route.abort()
                        return
                except Exception:
                    pass
                if route.request.resource_type in BLOCK_TYPES:
                    route.abort()
                else:
                    route.continue_()

            page.route('**/*', handle_route)

            while to_visit and fetched < max_pages:
                if stop_flag.value:
                    pipe_conn.send(('LOG', '⏹ クロールを中断しました。'))
                    break

                if high_error_rate():
                    pipe_conn.send(('LOG',
                        f'\n⚠️ エラー率が高すぎます（直近{ERROR_RATE_WINDOW}件中'
                        f'{recent_results.count(False)}件失敗）。クロールを中断します。'))
                    break

                url = to_visit.pop(0)
                if url in visited or is_excluded(url):
                    continue

                # SSRF対策: ループ内でもURLの安全チェック
                if not _is_safe_url(url):
                    pipe_conn.send(('LOG', f'  ブロック (プライベートIP/不正スキーム): {url}'))
                    visited.add(url)
                    continue

                # robots.txt の Disallow チェック
                if rp and not rp.can_fetch('*', url):
                    pipe_conn.send(('LOG', f'  robots.txt 除外: {url}'))
                    visited.add(url)
                    continue
                visited.add(url)
                fetched += 1

                try:
                    resp = _goto(page, url)

                    if not resp:
                        pipe_conn.send(('LOG', f'  スキップ (no resp): {url}'))
                        record_result(False)
                        continue

                    # X-Robots-Tag ヘッダーチェック
                    x_robots = resp.headers.get('x-robots-tag', '').lower()
                    xr_noindex = 'noindex' in x_robots
                    xr_nofollow = 'nofollow' in x_robots

                    # 429: バックオフして1回再試行
                    if resp.status == 429:
                        retry_after = parse_retry_after(resp.headers.get('retry-after'))
                        # 誤設定サーバーの極端な値でハングしないよう 30〜300秒に制限
                        wait = min(max(retry_after, 30), 300)
                        pipe_conn.send(('LOG', f'  ⏳ 429 レート制限: {wait}秒待機後に再試行 {url}'))
                        time.sleep(wait)
                        resp = _goto(page, url)
                        if not resp or resp.status != 200:
                            pipe_conn.send(('LOG', f'  スキップ (再試行失敗 {resp.status if resp else "no resp"}): {url}'))
                            record_result(False)
                            continue

                    # 500/503: 指数バックオフで最大3回リトライ
                    if resp and resp.status in (500, 503):
                        succeeded = False
                        for attempt in range(1, 4):
                            wait = min(2 ** attempt, 60)
                            pipe_conn.send(('LOG', f'  ⏳ {resp.status} サーバーエラー: {wait}秒後にリトライ ({attempt}/3) {url}'))
                            time.sleep(wait)
                            resp = _goto(page, url)
                            if resp and resp.status == 200:
                                succeeded = True
                                break
                        if not succeeded:
                            pipe_conn.send(('LOG', f'  スキップ (リトライ失敗): {url}'))
                            record_result(False)
                            continue

                    if not resp or resp.status != 200:
                        pipe_conn.send(('LOG', f'  スキップ ({resp.status if resp else "no resp"}): {url}'))
                        record_result(False)
                        continue

                    # リダイレクト検出（同一の正規化ルールで比較）
                    final_url = normalize_url(page.url)
                    if final_url != url:
                        pipe_conn.send(('LOG', f'  リダイレクト除外: {url} → {page.url}'))
                        record_result(True)
                        if crawl_delay > 0:
                            time.sleep(crawl_delay)
                        continue

                    # meta robots チェック
                    meta_noindex = False
                    meta_nofollow = False
                    try:
                        content = page.get_attribute('meta[name="robots"]', 'content') or ''
                        content = content.lower()
                        meta_noindex = 'noindex' in content
                        meta_nofollow = 'nofollow' in content
                    except Exception:
                        pass

                    add_to_sitemap = not (xr_noindex or meta_noindex)
                    follow_links = not (xr_nofollow or meta_nofollow)

                    if xr_noindex or meta_noindex:
                        pipe_conn.send(('LOG', f'  noindex 除外 (サイトマップ非登録): {url}'))

                    # canonical URL チェック
                    sitemap_url = url
                    try:
                        canonical = page.get_attribute('link[rel="canonical"]', 'href') or ''
                        canonical = canonical.strip()
                        if canonical:
                            # 相対 canonical を現在ページ基準で絶対URLへ解決
                            canonical = normalize_url(urljoin(page.url, canonical))
                        if canonical and canonical != url:
                            p_can = urlparse(canonical)
                            if p_can.netloc == domain and p_can.scheme in ('http', 'https'):
                                pipe_conn.send(('LOG', f'  canonical: {url} → {canonical}'))
                                sitemap_url = canonical
                    except Exception:
                        pass

                    # lastmod 取得
                    lastmod = None
                    try:
                        lm_header = resp.headers.get('last-modified', '')
                        if lm_header:
                            for fmt in ['%a, %d %b %Y %H:%M:%S %Z', '%a, %d %b %Y %H:%M:%S GMT']:
                                try:
                                    lastmod = datetime.strptime(lm_header, fmt).strftime('%Y-%m-%d')
                                    break
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    if not lastmod:
                        lastmod = datetime.now().strftime('%Y-%m-%d')

                    if add_to_sitemap and sitemap_url not in url_data:
                        url_data[sitemap_url] = lastmod
                        pipe_conn.send(('LOG', f'[{len(url_data):>5}] {sitemap_url}'))

                    # リンク抽出（nofollow でなければ）
                    if follow_links:
                        try:
                            anchors = page.eval_on_selector_all(
                                'a[href]', 'els => els.map(e => e.href)'
                            )
                            for abs_url in anchors:
                                abs_url = normalize_url(abs_url)
                                if not abs_url:
                                    continue
                                p = urlparse(abs_url)
                                if (p.netloc == domain
                                        and abs_url not in visited
                                        and abs_url not in to_visit
                                        and p.scheme in ('http', 'https')):
                                    to_visit.append(abs_url)
                        except Exception:
                            pass

                    record_result(True)

                    if crawl_delay > 0:
                        time.sleep(crawl_delay)

                except Exception as e:
                    pipe_conn.send(('LOG', f'  エラー: {url} → {e}'))
                    record_result(False)

            browser.close()

    except Exception as e:
        pipe_conn.send(('ERROR', f'クローラー起動エラー: {e}'))
        pipe_conn.close()
        return

    pipe_conn.send(('LOG', f'\n✅ クロール完了: {len(url_data)} URL 収集'))
    pipe_conn.send(('DONE', url_data))
    pipe_conn.close()


# ─────────────────────────────────────────────
# サイトマップ書き出し
# ─────────────────────────────────────────────
def _encode_loc(url):
    """サイトマップ仕様(IRI)に従い loc を percent-encode する。
    既存の %xx は safe='%' で二重エンコードしないようにする。
    国際化ドメイン(IDN)は Punycode(IDNA) へ変換する。"""
    parts = urlsplit(url)
    # hostname/port 属性を使い IPv6 アドレスやポート有無を安全に扱う
    hostname = parts.hostname
    if hostname:
        try:
            encoded_host = hostname.encode('idna').decode('ascii')
        except Exception:
            encoded_host = hostname
        if ':' in encoded_host and not encoded_host.startswith('['):
            netloc = f'[{encoded_host}]'   # IPv6 はブラケットで囲む
        else:
            netloc = encoded_host
        if parts.port is not None:
            netloc = f'{netloc}:{parts.port}'
    else:
        netloc = parts.netloc
    path = quote(parts.path, safe="/%:@!$&'()*+,;=~-._")
    query = quote(parts.query, safe="%:@!$&'()*+,;=~-._/?")
    return urlunsplit((parts.scheme, netloc, path, query, ''))


def build_sitemap_xml(urls_data, priority, changefreq):
    root = ET.Element('urlset')
    root.set('xmlns', 'http://www.sitemaps.org/schemas/sitemap/0.9')
    for url, lastmod in urls_data:
        url_el = ET.SubElement(root, 'url')
        ET.SubElement(url_el, 'loc').text = _encode_loc(url)
        if lastmod:
            ET.SubElement(url_el, 'lastmod').text = lastmod
        ET.SubElement(url_el, 'changefreq').text = changefreq
        ET.SubElement(url_el, 'priority').text = str(priority)
    xml_str = minidom.parseString(ET.tostring(root, encoding='unicode')).toprettyxml(indent='  ')
    return '\n'.join(l for l in xml_str.splitlines() if l.strip())


def build_sitemap_index(sitemap_urls):
    root = ET.Element('sitemapindex')
    root.set('xmlns', 'http://www.sitemaps.org/schemas/sitemap/0.9')
    for su in sitemap_urls:
        sm = ET.SubElement(root, 'sitemap')
        ET.SubElement(sm, 'loc').text = su
        ET.SubElement(sm, 'lastmod').text = datetime.now().strftime('%Y-%m-%d')
    xml_str = minidom.parseString(ET.tostring(root, encoding='unicode')).toprettyxml(indent='  ')
    return '\n'.join(l for l in xml_str.splitlines() if l.strip())


def save_sitemaps(url_data, out_dir, base_url_for_index, priority, changefreq, chunk_size=50000):
    items = list(url_data.items())
    saved_files = []
    if len(items) <= chunk_size:
        xml = build_sitemap_xml(items, priority, changefreq)
        path = os.path.join(out_dir, 'sitemap.xml')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(xml)
        saved_files.append(path)
    else:
        chunks = [items[i:i+chunk_size] for i in range(0, len(items), chunk_size)]
        sitemap_urls = []
        for idx, chunk in enumerate(chunks, 1):
            fname = f'sitemap{idx}.xml'
            xml = build_sitemap_xml(chunk, priority, changefreq)
            path = os.path.join(out_dir, fname)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(xml)
            saved_files.append(path)
            sitemap_urls.append(f"{base_url_for_index.rstrip('/')}/{fname}")
        idx_xml = build_sitemap_index(sitemap_urls)
        idx_path = os.path.join(out_dir, 'sitemap_index.xml')
        with open(idx_path, 'w', encoding='utf-8') as f:
            f.write(idx_xml)
        saved_files.insert(0, idx_path)
    return saved_files


# ─────────────────────────────────────────────
# GUIアプリ
# ─────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("XML サイトマップ生成ツール")
        self.geometry("820x700")
        self.resizable(True, True)
        self.configure(bg='#f5f5f5')

        self.crawl_process = None
        self.stop_flag = None
        self.log_queue = queue.Queue()
        self.url_data = {}

        self._build_ui()
        self._poll_log()

    def _build_ui(self):
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('TLabelframe.Label', font=('Meiryo', 10, 'bold'))
        style.configure('Accent.TButton', font=('Meiryo', 10, 'bold'))
        pad = {'padx': 10, 'pady': 5}

        frm_crawl = ttk.LabelFrame(self, text='🌐 クロール設定')
        frm_crawl.pack(fill='x', **pad)
        ttk.Label(frm_crawl, text='対象URL:').grid(row=0, column=0, sticky='w', padx=6, pady=4)
        self.var_url = tk.StringVar(value='https://')
        ttk.Entry(frm_crawl, textvariable=self.var_url, width=55).grid(row=0, column=1, columnspan=3, sticky='ew', padx=4, pady=4)
        ttk.Label(frm_crawl, text='最大ページ数:').grid(row=1, column=0, sticky='w', padx=6, pady=4)
        self.var_max = tk.IntVar(value=5000)
        ttk.Spinbox(frm_crawl, from_=1, to=100000, textvariable=self.var_max, width=10).grid(row=1, column=1, sticky='w', padx=4)
        ttk.Label(frm_crawl, text='クロール間隔(秒):').grid(row=1, column=2, sticky='w', padx=6)
        self.var_delay = tk.DoubleVar(value=1.0)
        ttk.Spinbox(frm_crawl, from_=0, to=5, increment=0.1, textvariable=self.var_delay, width=8, format='%.1f').grid(row=1, column=3, sticky='w', padx=4)
        frm_crawl.columnconfigure(1, weight=1)

        frm_excl = ttk.LabelFrame(self, text='🚫 除外URLパターン（正規表現・1行1パターン）')
        frm_excl.pack(fill='x', **pad)
        self.txt_exclude = scrolledtext.ScrolledText(frm_excl, height=4, font=('Consolas', 9))
        self.txt_exclude.pack(fill='x', padx=6, pady=4)
        self.txt_exclude.insert('end', '/wp-admin/\n/wp-login\n\\.(pdf|jpg|png|gif|zip)$\n')

        frm_sm = ttk.LabelFrame(self, text='⚙️ サイトマップ設定')
        frm_sm.pack(fill='x', **pad)
        ttk.Label(frm_sm, text='changefreq:').grid(row=0, column=0, sticky='w', padx=6, pady=4)
        self.var_changefreq = tk.StringVar(value='weekly')
        ttk.Combobox(frm_sm, textvariable=self.var_changefreq, width=12,
                     values=['always','hourly','daily','weekly','monthly','yearly','never'],
                     state='readonly').grid(row=0, column=1, sticky='w', padx=4)
        ttk.Label(frm_sm, text='priority:').grid(row=0, column=2, sticky='w', padx=6)
        self.var_priority = tk.DoubleVar(value=0.8)
        ttk.Spinbox(frm_sm, from_=0.0, to=1.0, increment=0.1, textvariable=self.var_priority,
                    width=8, format='%.1f').grid(row=0, column=3, sticky='w', padx=4)
        ttk.Label(frm_sm, text='分割サイズ(件):').grid(row=0, column=4, sticky='w', padx=6)
        self.var_chunk = tk.IntVar(value=50000)
        ttk.Spinbox(frm_sm, from_=1000, to=50000, increment=1000, textvariable=self.var_chunk,
                    width=10).grid(row=0, column=5, sticky='w', padx=4)

        frm_out = ttk.LabelFrame(self, text='💾 出力先フォルダ')
        frm_out.pack(fill='x', **pad)
        self.var_outdir = tk.StringVar(value=os.path.expanduser('~/Desktop'))
        ttk.Entry(frm_out, textvariable=self.var_outdir, width=55).grid(row=0, column=0, sticky='ew', padx=6, pady=4)
        ttk.Button(frm_out, text='参照...', command=self._browse_dir).grid(row=0, column=1, padx=6)
        frm_out.columnconfigure(0, weight=1)

        frm_btn = ttk.Frame(self)
        frm_btn.pack(fill='x', padx=10, pady=6)
        self.btn_start = ttk.Button(frm_btn, text='▶ クロール開始', command=self._start_crawl, style='Accent.TButton')
        self.btn_start.pack(side='left', padx=4)
        self.btn_stop = ttk.Button(frm_btn, text='⏹ 中断', command=self._stop_crawl, state='disabled')
        self.btn_stop.pack(side='left', padx=4)
        self.btn_save = ttk.Button(frm_btn, text='💾 XML 保存', command=self._save_xml, state='disabled')
        self.btn_save.pack(side='left', padx=4)
        self.btn_clear = ttk.Button(frm_btn, text='🗑 クリア', command=self._clear)
        self.btn_clear.pack(side='right', padx=4)
        self.lbl_count = ttk.Label(frm_btn, text='収集URL: 0件', foreground='#444')
        self.lbl_count.pack(side='right', padx=10)

        self.progress = ttk.Progressbar(self, mode='indeterminate')
        self.progress.pack(fill='x', padx=10, pady=2)

        frm_log = ttk.LabelFrame(self, text='📋 ログ')
        frm_log.pack(fill='both', expand=True, padx=10, pady=5)
        self.txt_log = scrolledtext.ScrolledText(frm_log, font=('Consolas', 9),
                                                  bg='#1e1e1e', fg='#d4d4d4',
                                                  insertbackground='white')
        self.txt_log.pack(fill='both', expand=True, padx=4, pady=4)

    def _browse_dir(self):
        d = filedialog.askdirectory(title='出力先フォルダを選択')
        if d:
            self.var_outdir.set(d)

    def _start_crawl(self):
        url = self.var_url.get().strip()
        parsed = urlparse(url)
        if parsed.scheme.lower() not in ('http', 'https') or not parsed.netloc:
            messagebox.showerror('エラー', '有効なURLを入力してください（http:// または https:// + ドメイン）。')
            return

        self.url_data = {}
        self.btn_start.config(state='disabled')
        self.btn_stop.config(state='normal')
        self.btn_save.config(state='disabled')
        self.progress.start(10)
        self._log('── クロール開始 ──────────────────────')

        exclude_patterns = self.txt_exclude.get('1.0', 'end').splitlines()

        parent_conn, child_conn = multiprocessing.Pipe(duplex=False)
        self.stop_flag = multiprocessing.Value('b', 0)

        self.crawl_process = multiprocessing.Process(
            target=crawler_process,
            args=(url, self.var_max.get(), self.var_delay.get(),
                  exclude_patterns, child_conn, self.stop_flag),
            daemon=True
        )
        self.crawl_process.start()
        child_conn.close()

        def read_pipe():
            while True:
                try:
                    msg_type, payload = parent_conn.recv()
                    if msg_type == 'LOG':
                        self.log_queue.put(payload)
                    elif msg_type == 'DONE':
                        self.url_data = payload
                        self.after(0, self._crawl_done)
                        break
                    elif msg_type == 'ERROR':
                        self.log_queue.put(f'❌ {payload}')
                        self.after(0, self._crawl_done)
                        break
                except EOFError:
                    self.after(0, self._crawl_done)
                    break
                except Exception as e:
                    self.log_queue.put(f'パイプエラー: {e}')
                    self.after(0, self._crawl_done)
                    break
            parent_conn.close()

        threading.Thread(target=read_pipe, daemon=True).start()

    def _stop_crawl(self):
        if self.stop_flag:
            self.stop_flag.value = 1

    def _crawl_done(self):
        self.progress.stop()
        self.btn_start.config(state='normal')
        self.btn_stop.config(state='disabled')
        count = len(self.url_data)
        self.lbl_count.config(text=f'収集URL: {count:,}件')
        if count > 0:
            self.btn_save.config(state='normal')
            self._log(f'\n💡 {count:,} 件収集しました。「XML 保存」ボタンで出力できます。')

    def _save_xml(self):
        if not self.url_data:
            messagebox.showwarning('警告', 'URLが収集されていません。')
            return
        out_dir = self.var_outdir.get().strip()
        if not os.path.isdir(out_dir):
            messagebox.showerror('エラー', '出力先フォルダが存在しません。')
            return
        base_url = self.var_url.get().strip()
        try:
            saved = save_sitemaps(
                url_data=self.url_data,
                out_dir=out_dir,
                base_url_for_index=base_url,
                priority=round(self.var_priority.get(), 1),
                changefreq=self.var_changefreq.get(),
                chunk_size=self.var_chunk.get(),
            )
            self._log('\n── 保存完了 ─────────────────────────')
            for f in saved:
                self._log(f'  📄 {f}')
            messagebox.showinfo('完了', f'{len(saved)}ファイル保存しました。\n\n' + '\n'.join(saved))
        except Exception as e:
            messagebox.showerror('保存エラー', str(e))

    def _clear(self):
        self.url_data = {}
        self.txt_log.delete('1.0', 'end')
        self.lbl_count.config(text='収集URL: 0件')
        self.btn_save.config(state='disabled')

    def _poll_log(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self._log(msg)
                if msg.startswith('['):
                    try:
                        n = int(msg.split(']')[0].strip('[').strip())
                        self.lbl_count.config(text=f'収集URL: {n:,}件')
                    except Exception:
                        pass
        except queue.Empty:
            pass
        self.after(150, self._poll_log)

    MAX_LOG_LINES = 5000

    def _log(self, msg):
        self.txt_log.insert('end', msg + '\n')
        # ログ肥大化を防ぐため上限行数を超えたら古い行を削除
        line_count = int(self.txt_log.index('end-1c').split('.')[0])
        if line_count > self.MAX_LOG_LINES:
            # delete の終了インデックスは排他的なため +1 して古い行を確実に削除
            self.txt_log.delete('1.0', f'{line_count - self.MAX_LOG_LINES + 1}.0')
        self.txt_log.see('end')


# ─────────────────────────────────────────────
if __name__ == '__main__':
    multiprocessing.freeze_support()  # PyInstaller exe化に必要
    app = App()
    app.mainloop()
