# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: one-folder, windowed build (starts faster at logon than --onefile and is
# less likely to trip antivirus heuristics).  Build with:  pyinstaller --noconfirm --clean callcompanion.spec

EXCLUDES = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQuick3D",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtRemoteObjects", "PySide6.QtPositioning", "PySide6.QtLocation",
    "tkinter",
]

a = Analysis(
    ["main.py"],
    pathex=["."],
    hiddenimports=["lxml.etree", "lxml._elementpath"],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="CallCompanion",
    console=False,      # no console window
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, upx=False, name="CallCompanion")
