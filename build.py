"""SeekSeek 빌드 스크립트

사용법:
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

ROOT     = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(ROOT, "dist")
BUILD_DIR = os.path.join(DIST_DIR, "SeekSeek")

# ── 버전은 단일 소스 ──────────────────────────────────────────────────────────
VERSION = "1.0.0"

PYINSTALLER = os.path.join(ROOT, ".venv", "Scripts", "pyinstaller.exe")
ISCC_CANDIDATES = [
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
]


def run(cmd: list[str], **kwargs):
    print(f"\n▶ {' '.join(cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        print(f"❌ 명령 실패 (exit {result.returncode})")
        sys.exit(result.returncode)


def build_exe():
    """PyInstaller로 단일 폴더 빌드."""
    run([PYINSTALLER, "--clean", "--noconfirm", "seekseek.spec"], cwd=ROOT)
    print(f"✅ EXE 빌드 완료 → {BUILD_DIR}")


def build_portable():
    """dist/SeekSeek 폴더를 ZIP으로 압축."""
    if not os.path.isdir(BUILD_DIR):
        print("dist/SeekSeek 없음, EXE 먼저 빌드합니다.")
        build_exe()

    zip_path = os.path.join(DIST_DIR, f"SeekSeek-{VERSION}-Portable.zip")
    print(f"\n▶ 포터블 ZIP 생성 → {zip_path}")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for dirpath, _, filenames in os.walk(BUILD_DIR):
            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                arcname = os.path.join("SeekSeek", os.path.relpath(fpath, BUILD_DIR))
                zf.write(fpath, arcname)
    size_mb = os.path.getsize(zip_path) / 1024 / 1024
    print(f"✅ 포터블 ZIP 완료 → {zip_path} ({size_mb:.1f} MB)")


def build_installer():
    """Inno Setup으로 인스톨러 빌드."""
    iscc = next((p for p in ISCC_CANDIDATES if os.path.isfile(p)), None)
    if not iscc:
        print("⚠ Inno Setup(ISCC.exe)을 찾을 수 없습니다.")
        print("  https://jrsoftware.org/isinfo.php 에서 설치하세요.")
        return

    # iss 파일의 버전을 현재 VERSION으로 패치
    iss_path = os.path.join(ROOT, "installer.iss")
    run([iscc, f"/DMyAppVersion={VERSION}", iss_path])
    out = os.path.join(DIST_DIR, f"SeekSeek-{VERSION}-Setup.exe")
    size_mb = os.path.getsize(out) / 1024 / 1024 if os.path.isfile(out) else 0
    print(f"✅ 인스톨러 완료 → {out} ({size_mb:.1f} MB)")


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
