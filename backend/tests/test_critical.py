"""PageRank panel: GET /critical-services on a real FalkorDB."""
import pytest
from fastapi.testclient import TestClient

import api


@pytest.fixture
def client(fresh):
    return TestClient(api.app)


def test_critical_services_ranked_by_pagerank(client):
    res = client.get("/critical-services?limit=5").json()
    assert res["algorithm"] == "pageRank"
    assert "algo.pageRank" in res["cypher"]
    rows = res["services"]
    assert len(rows) == 5
    assert [r["rank"] for r in rows] == [1, 2, 3, 4, 5]
    scores = [r["score"] for r in rows]
    assert scores == sorted(scores, reverse=True)
    # catalog-db is used by productcatalog, recommendation, checkout and frontend
    assert rows[0]["service"] == "catalog-db"
    assert rows[0]["dependents"] == 4
    assert rows[0]["team"] == "Commerce"


def test_critical_services_limit_is_clamped(client):
    assert len(client.get("/critical-services?limit=999").json()["services"]) == 15
    assert len(client.get("/critical-services?limit=0").json()["services"]) == 1
