# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: one-folder, windowed build (starts faster at logon than --onefile and is less likely to trip
# antivirus heuristics). The installer (installer.iss) packs this folder into one setup .exe.
# Build with build.ps1, or by hand:  pyinstaller --noconfirm --clean callcompanion.spec
#
# Output: dist\CallCompanion\CallCompanion.exe  +  _internal\ (Python runtime, Qt, assets\app.ico)
# build.ps1 then adds config.json and extension\ next to the exe (they are user-visible / user-editable files).
import os
import re

ROOT = SPECPATH
VERSION = re.search(r'__version__\s*=\s*"([^"]+)"', open(os.path.join(ROOT, "version.py"), encoding="utf-8").read()).group(1)
_core = [int(x) for x in re.match(r"(\d+)\.(\d+)\.(\d+)", VERSION).groups()]
NUMERIC = tuple(_core + [0])

# Windows file properties (Explorer > Properties > Details, Apps & Features).
os.makedirs(os.path.join(ROOT, "build"), exist_ok=True)
VERSION_FILE = os.path.join(ROOT, "build", "version_info.txt")
with open(VERSION_FILE, "w", encoding="utf-8") as fh:
    fh.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={NUMERIC}, prodvers={NUMERIC}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Dékány Richárd'),
      StringStruct('LegalCopyright', '© 2026 Dékány Richárd'),
      StringStruct('FileDescription', 'Call Companion - Bria / MiniCRM caller pop-up'),
      StringStruct('FileVersion', '{VERSION}'),
      StringStruct('InternalName', 'CallCompanion'),
      StringStruct('OriginalFilename', 'CallCompanion.exe'),
      StringStruct('ProductName', 'Call Companion'),
      StringStruct('ProductVersion', '{VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""")

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
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    # Everything under assets\ (icons, future overlay templates) -> _internal\assets, found via config.resource_path().
    datas=[(os.path.join(ROOT, "assets"), "assets")],
    hiddenimports=["lxml.etree", "lxml._elementpath", "updater"],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="CallCompanion",
    icon=os.path.join(ROOT, "assets", "app.ico"),
    version=VERSION_FILE,
    console=False,      # no console window
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, upx=False, name="CallCompanion")
