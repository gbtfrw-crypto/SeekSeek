"""SeekSeek 빌드 스크립트

사용법
    python build.py              # 인스톨러 + 포터블 ZIP 모두 빌드
    python build.py --portable   # 포터블 ZIP만
    python build.py --installer  # 인스톨러만 (Inno Setup 필요)
"""
import os
import sys
import shutil
import argparse
import subprocess
import zipfile
import ast

ROOT     = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(ROOT, "dist")
BUILD_DIR = os.path.join(DIST_DIR, "SeekSeek")

# 현재 버전은 단일 상수로 관리
VERSION = "1.0.0"

ISCC_CANDIDATES = [
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
]


def run(cmd: list[str], **kwargs):
    print(f"\n>> {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        print(f"[ERR] 명령 실패 (exit {result.returncode})")
        sys.exit(result.returncode)


def resolve_pyinstaller_cmd() -> list[str]:
    """Return a PyInstaller command compatible with the current Python env."""
    try:
        __import__("PyInstaller")
        return [sys.executable, "-m", "PyInstaller"]
    except ImportError:
        pyinstaller = shutil.which("pyinstaller")
        if pyinstaller:
            return [pyinstaller]

        local = os.path.join(ROOT, ".venv", "Scripts", "pyinstaller.exe")
        if os.path.isfile(local):
            return [local]

        print("PyInstaller not found.")
        print(f"  Current Python: {sys.executable}")
        print("  Install and retry:")
        print("  python -m pip install pyinstaller")
        sys.exit(1)


# 빌드 결과에서 제거할 불필요 파일/폴더
# Qt 이미지 형식 플러그인 (ico/svg만 필요)
_REMOVE_QT_IMAGE_PLUGINS = {
    "qjpeg.dll", "qwebp.dll", "qtiff.dll", "qicns.dll",
    "qgif.dll", "qwbmp.dll", "qtga.dll", "qpdf.dll",
}
# 불필요 DLL/플러그인
_REMOVE_FILES = {
    "Qt6Pdf.dll",           # PyMuPDF 자체 PDF 엔진 사용
    "libssl-3.dll",         # 오프라인 앱, SSL 불필요
    "libcrypto-3.dll",      # crypto 불필요
    "qtuiotouchplugin.dll", # 터치 입력 불필요
    "qoffscreen.dll",       # 오프스크린 렌더 불필요
    "qminimal.dll",         # 최소 플랫폼 드라이버 불필요
    "_ssl.pyd",             # Python SSL 모듈
}
# 통째로 제거할 폴더
_REMOVE_DIRS = [
    os.path.join("_internal", "lxml", "html"),
    os.path.join("_internal", "lxml", "sax.cp311-win_amd64.pyd"),
]


def strip_bloat():
    """빌드 결과에서 불필요한 파일/폴더를 제거해 용량을 줄인다."""
    removed = 0
    for dirpath, dirnames, filenames in os.walk(BUILD_DIR):
        for fname in filenames:
            if fname in _REMOVE_QT_IMAGE_PLUGINS or fname in _REMOVE_FILES:
                fpath = os.path.join(dirpath, fname)
                os.remove(fpath)
                print(f"  제거: {os.path.relpath(fpath, BUILD_DIR)}")
                removed += 1
    for rel in _REMOVE_DIRS:
        target = os.path.join(BUILD_DIR, rel)
        if os.path.isdir(target):
            shutil.rmtree(target)
            print(f"  제거 (폴더): {rel}")
            removed += 1
    print(f"[OK] 불필요 파일 {removed}개 제거 완료")


def _load_pyz_modules() -> set[str]:
    """Read bundled module names from PyInstaller's PYZ-00.toc."""
    toc_path = os.path.join(ROOT, "build", "seekseek", "PYZ-00.toc")
    if not os.path.isfile(toc_path):
        return set()
    try:
        with open(toc_path, "r", encoding="utf-8", errors="ignore") as f:
            toc = ast.literal_eval(f.read())
        if not isinstance(toc, tuple) or len(toc) < 2 or not isinstance(toc[1], list):
            return set()
        return {
            entry[0]
            for entry in toc[1]
            if isinstance(entry, tuple)
            and len(entry) >= 3
            and isinstance(entry[0], str)
            and entry[2] == "PYMODULE"
        }
    except Exception:
        return set()


def verify_bundled_packages():
    """Verify key packages are included either as files or in PYZ archive."""
    internal = os.path.join(BUILD_DIR, "_internal")
    checks = {
        "pptx": os.path.join(internal, "pptx"),
        "lxml": os.path.join(internal, "lxml"),
        "PIL": os.path.join(internal, "PIL"),
        "openpyxl": os.path.join(internal, "openpyxl"),
        "docx": os.path.join(internal, "docx"),
        "fitz": os.path.join(internal, "fitz"),
        "olefile": os.path.join(internal, "olefile"),
    }
    pyz_modules = _load_pyz_modules()
    pymupdf_path = os.path.join(internal, "pymupdf")

    print("\n[INFO] 번들 패키지 검증:")
    all_ok = True
    for name, path in checks.items():
        if os.path.isdir(path):
            count = sum(1 for _, _, fs in os.walk(path) for _ in fs)
            print(f"  [OK] {name}: {count}개 파일")
        elif os.path.isfile(path + ".pyc") or os.path.isfile(path + ".pyd"):
            print(f"  [OK] {name}: 단일 파일")
        elif name in pyz_modules:
            print(f"  [OK] {name}: PYZ 아카이브 포함")
        elif name == "fitz" and os.path.isdir(pymupdf_path):
            count = sum(1 for _, _, fs in os.walk(pymupdf_path) for _ in fs)
            print(f"  [OK] {name}: pymupdf ({count}개 파일)")
        else:
            print(f"  [ERR] {name}: 누락!")
            all_ok = False

    pptx_templates = os.path.join(internal, "pptx", "templates")
    if os.path.isdir(pptx_templates):
        files = os.listdir(pptx_templates)
        print(f"  [OK] pptx/templates: {files}")
    else:
        print("  [ERR] pptx/templates: 누락!")
        all_ok = False

    if not all_ok:
        print("[WARN] 일부 패키지가 누락되었습니다!")
    return all_ok

def build_exe():
    """PyInstaller로 EXE 폴더를 빌드하고 불필요 파일을 제거한다."""
    pyinstaller_cmd = resolve_pyinstaller_cmd()
    run(pyinstaller_cmd + ["--clean", "--noconfirm", "seekseek.spec"], cwd=ROOT)
    verify_bundled_packages()
    strip_bloat()
    verify_bundled_packages()
    size_mb = sum(
        os.path.getsize(os.path.join(dp, f))
        for dp, _, fs in os.walk(BUILD_DIR) for f in fs
    ) / 1024 / 1024
    print(f"[OK] EXE 빌드 완료 -> {BUILD_DIR} ({size_mb:.0f} MB)")


def build_portable():
    """dist/SeekSeek 폴더를 포터블 ZIP으로 압축한다."""
    if not os.path.isdir(BUILD_DIR):
        print("dist/SeekSeek 없음, EXE 먼저 빌드합니다.")
        build_exe()

    zip_path = os.path.join(DIST_DIR, f"SeekSeek-{VERSION}-Portable.zip")
    print(f"\n[INFO] 포터블 ZIP 생성 -> {zip_path}")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for dirpath, _, filenames in os.walk(BUILD_DIR):
            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                arcname = os.path.join("SeekSeek", os.path.relpath(fpath, BUILD_DIR))
                zf.write(fpath, arcname)
    size_mb = os.path.getsize(zip_path) / 1024 / 1024
    print(f"[OK] 포터블 ZIP 완료 -> {zip_path} ({size_mb:.1f} MB)")


def build_installer():
    """Inno Setup으로 인스톨러를 빌드한다."""
    iscc = next((p for p in ISCC_CANDIDATES if os.path.isfile(p)), None)
    if not iscc:
        print("[WARN] Inno Setup(ISCC.exe)을 찾을 수 없습니다.")
        print("  https://jrsoftware.org/isinfo.php 에서 설치하세요.")
        return

    # iss 파일의 버전을 현재 VERSION으로 치환
    iss_path = os.path.join(ROOT, "installer.iss")
    run([iscc, f"/DMyAppVersion={VERSION}", iss_path])
    out = os.path.join(DIST_DIR, f"SeekSeek-{VERSION}-Setup.exe")
    size_mb = os.path.getsize(out) / 1024 / 1024 if os.path.isfile(out) else 0
    print(f"[OK] 인스톨러 완료 -> {out} ({size_mb:.1f} MB)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--portable",  action="store_true")
    parser.add_argument("--installer", action="store_true")
    args = parser.parse_args()

    build_exe()

    if args.portable:
        build_portable()
    elif args.installer:
        build_installer()
    else:
        build_portable()
        build_installer()


if __name__ == "__main__":
    main()

