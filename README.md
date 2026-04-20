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

## 注意事項

- 50,000件を超える場合は自動的に sitemap1.xml, sitemap2.xml... と分割され、sitemap_index.xml が生成されます
- クロール対象サイトの robots.txt は遵守してください
- クロール間隔を短くしすぎるとサーバーに負荷がかかります
- exe版は Chromium ブラウザを同梱するため、フォルダサイズが大きくなります（約300MB）
