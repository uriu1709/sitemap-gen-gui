# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['sitemap_generator.py'],
    pathex=[],
    binaries=[],
    datas=[('C:\\Users\\Works-60\\AppData\\Roaming\\Python\\Python314\\site-packages\\playwright\\driver', 'playwright/driver')],
    hiddenimports=['playwright', 'playwright.sync_api', 'playwright._impl', 'playwright._impl._driver'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SitemapGenerator',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    contents_directory='lib',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='SitemapGenerator',
)
