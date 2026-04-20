"""PyInstaller ビルドヘルパー - Playwright + Chromium を含めた exe を生成"""
import subprocess
import shutil
import os
import sys


def get_playwright_driver_dir():
    """Playwright ドライバーのディレクトリを取得"""
    from playwright._impl._driver import compute_driver_executable
    result = compute_driver_executable()
    # 新しいバージョンはタプル (node_exe, cli_js) を返す
    if isinstance(result, tuple):
        driver_exe = result[0]
    else:
        driver_exe = result
    return os.path.dirname(driver_exe)


def get_browsers_src():
    """ms-playwright ブラウザのインストール先を取得"""
    path = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'ms-playwright')
    if os.path.isdir(path):
        return path
    # フォールバック: PLAYWRIGHT_BROWSERS_PATH
    env_path = os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '')
    if env_path and os.path.isdir(env_path):
        return env_path
    return None


def main():
    print('Playwright driver path...')
    try:
        driver_dir = get_playwright_driver_dir()
        print(f'  Driver: {driver_dir}')
    except Exception as e:
        print(f'  [ERROR] Could not find Playwright driver: {e}')
        sys.exit(1)

    print('Browser path...')
    browsers_src = get_browsers_src()
    if not browsers_src:
        print('  [ERROR] ms-playwright browsers not found.')
        print('  Run: playwright install chromium')
        sys.exit(1)
    print(f'  Browsers: {browsers_src}')

    # PyInstaller でビルド
    print('Running PyInstaller...')
    add_data = f'{driver_dir}{os.pathsep}playwright/driver'
    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--noconfirm',
        '--name', 'SitemapGenerator',
        '--windowed',
        '--contents-directory', 'lib',
        '--add-data', add_data,
        '--hidden-import', 'playwright',
        '--hidden-import', 'playwright.sync_api',
        '--hidden-import', 'playwright._impl',
        '--hidden-import', 'playwright._impl._driver',
        'sitemap_generator.py',
    ]
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print('[ERROR] PyInstaller failed.')
        sys.exit(1)

    # ブラウザバイナリをコピー
    dest = os.path.join('dist', 'SitemapGenerator', 'browsers')
    print(f'Copying browsers to {dest}...')
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(browsers_src, dest)
    print('Done!')


if __name__ == '__main__':
    main()
