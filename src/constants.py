"""
Centralized constants for llmLibrarian. Shared across ingest, query, and CLI modules.
"""

# Storage
DB_PATH = "./my_brain_db"
LLMLI_COLLECTION = "llmli"

# Chunking
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 100

# Ingestion
ADD_BATCH_SIZE = 256
MAX_WORKERS = 8

# Query defaults
DEFAULT_N_RESULTS = 12
DEFAULT_MODEL = "llama3.1:8b"
# Keep snippets short enough for terminal readability while still showing enough context.
SNIPPET_MAX_LEN = 180
# Limit per-source chunks to avoid a single file dominating answers.
MAX_CHUNKS_PER_FILE = 4
# Distance threshold for default relevance filtering (tuned for Chroma distances ~0-2).
DEFAULT_RELEVANCE_MAX_DISTANCE = 2.0
