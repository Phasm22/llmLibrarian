import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Avoid color/style differences in test output formatting.
os.environ.setdefault("LLMLIBRARIAN_EDITOR_SCHEME", "file")
