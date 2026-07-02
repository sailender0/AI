# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Developer Activity Agent.

Prerequisites:
    pip install pywebview pystray pillow psutil requests keyring pyinstaller

Build:
    pyinstaller agent/agent.spec

Output: dist/DevActivityAgent.exe
"""
from pathlib import Path

ROOT = Path(SPECPATH).parent  # repo root

a = Analysis(
    [str(ROOT / "agent" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[
        # Keyring Windows backend
        "keyring.backends.Windows",
        "keyring.backends.fail",
        # Tray icon
        "pystray._win32",
        "PIL._tkinter_finder",
        # pywebview — Windows uses the WinForms + WebView2 backend
        "webview",
        "webview.platforms",
        "webview.platforms.winforms",
        # Standard library
        "psutil",
        "requests",
        "http.server",
        "threading",
        "socket",
        "winreg",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "pandas",
        "scipy",
        "IPython",
        "jupyter",
        "notebook",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="DevActivityAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # set to "agent/icon.ico" when available
)
