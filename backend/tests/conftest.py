"""Shared test setup.

The tests talk to a REAL FalkorDB (the one from docker-compose), but on a
separate graph called 'roottrace_test', so your demo graph 'roottrace' is never
touched. Gemini and Discord are always faked: no API key or webhook is used.

Run from the backend folder:  python -m pytest -v
"""
import os
import sys
from pathlib import Path

# Must be set BEFORE graph.py / agent.py are imported. load_dotenv() does not
# override variables that already exist, so your real .env values are ignored.
os.environ["ROOTTRACE_GRAPH"] = "roottrace_test"
os.environ["GEMINI_API_KEY"] = ""
os.environ["DISCORD_WEBHOOK_URL"] = ""
os.environ["GEMINI_BACKOFF_SECONDS"] = "0.01"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import graph  # noqa: E402
import seed  # noqa: E402

assert graph.GRAPH_NAME != "roottrace", "tests must never run on the demo graph"


def _falkordb_up():
    try:
        graph.get_graph().query("RETURN 1")
        return True
    except Exception:
        return False


if not _falkordb_up():
    pytest.skip("FalkorDB is not running (start it with: docker compose up -d)", allow_module_level=True)


@pytest.fixture
def fresh():
    """A clean, freshly seeded test graph for each test."""
    seed.seed()
    yield graph
