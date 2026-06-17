# XML サイトマップ生成ツール（Playwright版）

JS描画後のDOMからリンクを抽出し、XMLサイトマップを生成するGUIツールです。

## セットアップ（Python から実行）

1. Python 3.9以上をインストール（https://python.org）
2. 依存ライブラリをインストール：

```
pip install -r requirements.txt
playwright install chromium
```

3. 起動：

```
python sitemap_generator.py
```

## exe ビルド

`build_exe.bat` をダブルクリックで実行してください。
自動で以下を行います：

1. Playwright + PyInstaller をインストール
2. Chromium ブラウザをダウンロード
3. exe をビルド（`dist\SitemapGenerator\` フォルダ）
4. Chromium を `dist\SitemapGenerator\browsers\` にコピー

ビルド完了後、`dist\SitemapGenerator\SitemapGenerator.exe` を実行してください。

**配布時は `dist\SitemapGenerator\` フォルダごとコピーしてください。**

## 使い方

1. **対象URL** にクロール対象のサイトURL（例: https://example.com）を入力
2. **最大ページ数** でクロール上限を設定
3. **クロール間隔** でサーバー負荷を調整（推奨: 0.5秒以上）
4. **除外URLパターン** に除外したいURLの正規表現を入力（1行1パターン）
5. **changefreq / priority** を設定
6. **出力先フォルダ** を選択
7. **▶ クロール開始** をクリック
8. 完了後、**💾 XML 保存** をクリック

## 動作の特徴

- **JS描画対応**: ページ遷移後に JavaScript の描画完了（networkidle）を待ってから DOM を解析するため、SPA など動的にリンクを生成するサイトにも対応します（描画に不要な画像・CSS・フォント・メディアのみ通信をブロックします）。
- **クエリ文字列を保持**: `?page=2` などクエリの異なる URL は別ページとして収集します（フラグメント `#` のみ除去）。動的に無限の URL を生成するサイトでは「除外URLパターン」と「最大ページ数」で歯止めをかけてください。
- **最大ページ数**: 実際に取得を試みたページ数の上限です（noindex やリダイレクトのページも 1 件としてカウントします）。

## 注意事項

- 50,000件を超える場合は自動的に sitemap1.xml, sitemap2.xml... と分割され、sitemap_index.xml が生成されます
- クロール対象サイトの robots.txt は遵守してください（取得は10秒でタイムアウトし、未設置でも続行します）
- クロール間隔を短くしすぎるとサーバーに負荷がかかります
- exe版は Chromium ブラウザを同梱するため、フォルダサイズが大きくなります（約300MB）
