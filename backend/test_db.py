"""Quick check: can Python talk to FalkorDB?"""
from falkordb import FalkorDB

db = FalkorDB(host="localhost", port=6379)
g = db.select_graph("roottrace")
g.query("MERGE (:Service {name: 'test'})")
print(g.query("MATCH (s:Service) RETURN s.name").result_set)
g.query("MATCH (s:Service {name: 'test'}) DELETE s")
