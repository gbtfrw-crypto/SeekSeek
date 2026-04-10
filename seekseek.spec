# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 빌드 스펙 — SeekSeek"""

import sys
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules, collect_data_files

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=collect_dynamic_libs('PyMuPDF') + collect_dynamic_libs('lxml'),
    datas=[
        ('assets/icon.ico', 'assets'),
        ('stubs/PIL', 'PIL'),          # PIL 스텁 모듈 (pptx 임포트용)
    ] + collect_data_files('pptx') + collect_data_files('openpyxl') + collect_data_files('olefile') + collect_data_files('docx') + collect_data_files('fitz'),
    hiddenimports=[
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
        'lxml',
        'lxml.etree',
        'xml.etree.ElementTree',
        'zipfile',
        'zlib',
        'sqlite3',
    ] + collect_submodules('pptx') + collect_submodules('openpyxl') + collect_submodules('olefile') + collect_submodules('docx') + collect_submodules('fitz'),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        # Pillow — 앱에서 미사용 (PyMuPDF가 간접 의존하지만 직접 사용 안 함)
        'PIL._avif', 'PIL._webp', 'PIL._imaging',
        'PIL._imagingcms', 'PIL._imagingft', 'PIL._imagingmorph',
        # 과학/데이터 라이브러리
        'matplotlib', 'numpy', 'pandas', 'scipy', 'IPython',
        # 네트워크/보안 (오프라인 앱)
        'cryptography', 'ssl', '_ssl',
        # 기타 불필요
        'xmlrpc', 'ftplib', 'imaplib',
        'unittest', 'doctest', 'pdb', 'profile', 'pstats',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SeekSeek',
    debug=True,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,              # UPX 설치 후 True로 변경
    console=True,          # 콘솔 창 숨김
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/icon.ico',
    uac_admin=True,         # 관리자 권한 요청 (UAC manifest)
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='SeekSeek',
)
