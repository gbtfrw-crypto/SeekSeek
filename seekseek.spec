# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 빌드 스펙 — SeekSeek"""

import sys
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_all

block_cipher = None

pptx_datas, pptx_binaries, pptx_hidden = collect_all('pptx')

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=collect_dynamic_libs('PyMuPDF') + pptx_binaries,
    datas=[
        ('assets/icon.ico', 'assets'),
    ] + pptx_datas,
    hiddenimports=[
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
        'fitz',
        'olefile',
        'openpyxl',
        'docx',
        'lxml',
        'lxml.etree',
        'lxml._elementpath',
        'xml.etree.ElementTree',
        'zipfile',
        'zlib',
        'sqlite3',
    ] + pptx_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        # Pillow — 앱에서 미사용 (PyMuPDF가 간접 의존하지만 직접 사용 안 함)
        'PIL', 'PIL._avif', 'PIL._webp', 'PIL._imaging',
        'PIL._imagingcms', 'PIL._imagingft', 'PIL._imagingmorph',
        # 과학/데이터 라이브러리
        'matplotlib', 'numpy', 'pandas', 'scipy', 'IPython',
        # 네트워크/보안 (오프라인 앱)
        'cryptography', 'ssl', '_ssl',
        # lxml HTML 파싱 (pptx용 etree만 필요)
        'lxml.html', 'lxml.html._difflib', 'lxml.html.diff',
        'lxml.sax',
        # 기타 불필요
        'xmlrpc', 'email', 'http', 'ftplib', 'imaplib',
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
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,              # UPX 설치 후 True로 변경
    console=False,          # 콘솔 창 숨김
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
