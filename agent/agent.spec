# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Developer Activity Agent.

Build:
    pyinstaller agent/agent.spec

Output: dist/DevActivityAgent.exe  (~25-35 MB)

Excludes pywebview to keep size down on first build;
add it back once the basic exe is validated.
"""
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent  # repo root

a = Analysis(
    [str(ROOT / "agent" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "keyring.backends.Windows",
        "keyring.backends.fail",
        "pystray._win32",
        "PIL._tkinter_finder",
        "psutil",
        "requests",
        "webbrowser",
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
        "cryptography",
        "OpenSSL",
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
    console=False,          # no terminal window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,              # add icon.ico here when available
)
