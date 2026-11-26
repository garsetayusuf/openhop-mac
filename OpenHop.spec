# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['src/openhop_app.py'],
    pathex=[],
    binaries=[],
    datas=[('src/ui', 'ui'), ('assets', 'assets')],
    hiddenimports=['psutil'],
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
    name='OpenHop',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch='arm64',
    codesign_identity=None,
    entitlements_file=None,
    icon=['/Users/macbookpro/openhop-mac/assets/brand/openhop.icns'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='OpenHop',
)
app = BUNDLE(
    coll,
    name='OpenHop.app',
    icon='/Users/macbookpro/openhop-mac/assets/brand/openhop.icns',
    bundle_identifier=None,
)
