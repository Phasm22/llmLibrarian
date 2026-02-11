# PyInstaller spec for pal CLI binary
# Build: pyinstaller pal.spec
# Output: dist/pal (single file)

import os
block_cipher = None

a = Analysis(
    ['pal.py'],
    pathex=['src'],
    datas=[('archetypes.yaml', '.')],
    hiddenimports=[
        'ingest', 'indexer', 'state', 'style', 'constants', 'embeddings',
        'processors', 'reranker', 'load_config', 'silo_audit', 'floor',
        'query', 'query.core', 'query.intent', 'query.retrieval',
        'query.context', 'query.formatting', 'query.code_language',
        'query.trace', 'query.guardrails', 'query.project_count',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='pal',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)
