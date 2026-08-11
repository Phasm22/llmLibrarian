"""Default include/exclude patterns for file scanning.

Single source for both scanners: ``ingest`` (full pipeline) and
``ingest.watch_scan`` (watch daemons, deliberately free of chromadb/torch
imports). They previously carried byte-identical copies, which is a silent
drift hazard — a pattern added to one scanner but not the other means
``llmli add`` and the watcher disagree about what belongs in a silo.

Kept dependency-free so watch_scan stays lightweight.
"""

from __future__ import annotations

ADD_DEFAULT_INCLUDE = [
    "*.py", "*.ts", "*.tsx", "*.js", "*.go", "*.rs", "*.sh", "*.md", "*.txt",
    "*.yml", "*.yaml", "*.json", "*.csv", "*.xml", "*.html", "*.htm", "*.rst", "*.toml", "*.ini", "*.cfg", "*.sql",
    "*.pdf", "*.docx", "*.xlsx", "*.pptx",
    "*.png", "*.jpg", "*.jpeg", "*.heic", "*.heif", "*.tif", "*.tiff",
]

ADD_DEFAULT_EXCLUDE = [
    # Obsidian / journalLinker intent cortex lives under .../cortex/; keep out of retrieval silos.
    "/cortex/",
    "node_modules/", ".venv/", "venv/", "env/", "__pycache__/", "vendor", "dist", "build", ".git",
    "llmLibrarianVenv/", "site-packages/", "Old Firefox Data", "Firefox", ".app/",
    ".env", ".env.*", ".aws/", ".ssh/", "*.pem", "*.key", "secrets.json", "credentials.json", "credentials*.json",
    "pnpm-lock.yaml", "package-lock.json", "yarn.lock", "Pipfile.lock", "poetry.lock",
    "composer.lock", "Gemfile.lock", "Cargo.lock", "uv.lock",
    "my_brain_db/", "*.db", "*.sqlite", "*.sqlite3", "*.sqlite3-journal",
    # The vector store's own bookkeeping. "my_brain_db/" above only catches the
    # default location, but LLMLIBRARIAN_DB is configurable — with a DB
    # anywhere else, these .json artifacts match the *.json include rule and
    # the store indexes itself. That also self-feeds: ingesting rewrites the
    # manifest, which the watcher then sees as a change to re-ingest. Matched
    # by name so the location does not matter.
    "llmli_*.json", "image_artifacts/",
]
