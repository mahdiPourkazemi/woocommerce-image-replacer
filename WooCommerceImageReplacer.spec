# -*- mode: python ; coding: utf-8 -*-
# Build:  .\.venv-image-replacer\Scripts\python.exe -m PyInstaller WooCommerceImageReplacer.spec --noconfirm --distpath dist-image-replacer --workpath build-image-replacer
#
# Only the page file is bundled as data. The .env file is deliberately NOT
# listed: settings and keys are read from a file beside the finished .exe.
from PyInstaller.utils.hooks import copy_metadata

datas = [("app/image_replacer/ui/index.html", "app/image_replacer/ui")]
datas += copy_metadata("pywebview")

a = Analysis(
    ["woocommerce_image_replacer.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=["webview.platforms.winforms", "webview.platforms.edgechromium", "clr", "pythonnet"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # The product-creation app and its web stack are not part of this tool.
    excludes=["tkinter", "pytest", "app.test", "app.main", "app.api", "app.services", "app.ui",
              "fastapi", "uvicorn", "pydantic", "jinja2", "openpyxl",
              "PyQt5", "PyQt6", "PySide2", "PySide6", "webview.platforms.cef", "webview.platforms.qt"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="WooCommerceImageReplacer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
