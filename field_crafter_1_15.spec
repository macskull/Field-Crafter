# PyInstaller spec for the portable one-file Windows build.
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH)

runtime_data_names = (
    "homecoming_recipes.sqlite",
    "memory_recipe_aliases.json",
    "validation_report.txt",
    "validation_report.json",
    "release_data_summary.json",
    "release_database_info.json",
    "README.txt",
)

datas = [
    (str(ROOT / "assets" / "field_crafter.ico"), "assets"),
    (str(ROOT / "assets" / "field_crafter_icon_transparent.png"), "assets"),
]
datas += [(str(ROOT / "data" / name), "data") for name in runtime_data_names]
binaries = []
hiddenimports = []

for package in ("rapidocr", "onnxruntime", "tkinterdnd2"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

a = Analysis(
    [str(ROOT / "field_crafter_entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    a.binaries,
    a.datas,
    [],
    name="Field_Crafter_1.15",
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
    icon=str(ROOT / "assets" / "field_crafter.ico"),
)
