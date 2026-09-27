# Build on Windows: python -m PyInstaller --noconfirm --distpath dist/updated voxlate.spec
from pathlib import Path
import os
import sys

root = Path(SPECPATH)
datas = [(str(root / name), ".") for name in
         ("config.json", "requirements.txt", "requirements-prepare.txt", "README.md", "README.en.md", "TODO.md", "LICENSE", "THIRD_PARTY_NOTICES.md")]
datas.append((str(root / "assets/voxlate.ico"), "assets"))
datas += [(str(path), "docs/images") for path in (root / "docs/images").glob("*.png")]
datas += [(str(path), "docs") for path in (root / "docs").glob("*.md")]
for folder in ("voxlate", "scripts"):
    datas += [(str(path), folder) for path in (root / folder).glob("*.py")]

a = Analysis(
    [str(root / "voxlate_gui.py")], pathex=[str(root)], binaries=[], datas=datas,
    hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["torch", "torchaudio", "faster_whisper", "ctranslate2", "transformers",
              "indextts", "demucs", "numpy", "scipy", "matplotlib", "tkinter", "pytest", "PIL"],
    noarchive=False,
)
# PyInstaller collects every installed Qt GUI plugin, including unused PDF and
# GPL-only virtual-keyboard plugins. Voxlate uses native Windows input and does
# not render PDF, so do not redistribute those plugins or their private DLLs.
omitted = {"qpdf.dll", "Qt6Pdf.dll", "qtvirtualkeyboardplugin.dll", "Qt6VirtualKeyboard.dll"}
a.binaries = [item for item in a.binaries if Path(item[0]).name not in omitted]
# Never pick an unrelated application's OpenSSL from the developer's PATH.
for index, (dest, source, kind) in enumerate(a.binaries):
    if Path(dest).name.lower() in {"libcrypto-3-x64.dll", "libssl-3-x64.dll"}:
        runtime_dll = Path(sys.base_prefix) / "DLLs" / Path(dest).name
        if not runtime_dll.is_file():
            raise RuntimeError(f"Python runtime DLL missing: {runtime_dll.name}")
        a.binaries[index] = (dest, str(runtime_dll), kind)
pyz = PYZ(a.pure)
# The release directory keeps Qt/PySide libraries accessible for replacement.
# The existing local single-file build remains available for development.
options = dict(name="voxlate", debug=False, icon=str(root / "assets/voxlate.ico"),
               bootloader_ignore_signals=False, strip=False, upx=False,
               console=False, disable_windowed_traceback=False)
if os.environ.get("VOXLATE_RELEASE_BUILD") == "1":
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **options)
    collect = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="voxlate")
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], **options)
