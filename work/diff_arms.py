# -*- coding: utf-8 -*-
import json, sys
rep = json.load(open(sys.argv[1], encoding="utf-8"))
cur = {r["id"]: r for r in rep["arms"]["current"]["queries"]}
fix = {r["id"]: r for r in rep["arms"]["fixed"]["queries"]}
print("%-4s %-6s %-14s %-22s %-30s" % ("id", "cat", "verdict", "query", "current -> fixed"))
for qid in cur:
    a, b = cur[qid], fix[qid]
    if a["ids"] == b["ids"] and a["inject_chars"] == b["inject_chars"]:
        continue
    verdict = ""
    if not a["hit"] and b["hit"]:
        verdict = "FIXED-MISS"
    elif a["hit"] and not b["hit"]:
        verdict = "NEW-MISS!"
    elif a["n"] == 0 and b["n"] > 0:
        verdict = "was-empty"
    elif a["n"] > 0 and b["n"] == 0:
        verdict = "now-empty"
    elif not a["answerable"] and a["n"] == 0 and b["n"] > 0:
        verdict = "NEW-NOISE"
    elif not a["answerable"] and a["n"] > 0 and b["n"] == 0:
        verdict = "noise-gone"
    print("%-4s %-6s %-14s %-22s n %d->%d p %.2f->%.2f hit %s->%s ids %s->%s" % (
        qid, a["cat"], verdict, a["query"], a["n"], b["n"], a["precision_at_k"], b["precision_at_k"],
        a["hit"], b["hit"], a["ids"][:4], b["ids"][:4]))
