"""Test bootstrap: make the historical `import server` resolve to the packaged
module (`clexo.cli`), so the suite runs both from a bare checkout and after install
without rewriting every `server.*` reference."""
import sys
from pathlib import Path

# Repo root on path so `import clexo.cli` works from a bare checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import clexo.cli as _cli

sys.modules.setdefault("server", _cli)
