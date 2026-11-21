# -*- coding: utf-8 -*-
"""细看 current 臂：分数分布、噪声来源、近重复阈值校准。"""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE)); sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tests")); sys.path.insert(0, HERE)
import recall_common as rc
rc.install_stub()
from utils.text_process import _informative_terms, _recall_tokens, _jaccard

rep = json.load(open(sys.argv[1], encoding="utf-8"))
arm = sys.argv[2] if len(sys.argv) > 2 else "current"
rows = rep["arms"][arm]["queries"]

print("== 每条查询：n / top分 / 是否命中 ==")
for r in rows:
    top = r["scores"][0] if r["scores"] else None
    print("%-4s %-4s %-6s n=%d top=%s hit=%s p=%.2f inject=%d %s" % (
        r["id"], r["cat"], r["grader"], r["n"], top, r["hit"], r["precision_at_k"],
        r["inject_chars"], r["query"]))

print("\n== 可答 top 分 vs 不可答 top 分 ==")
ans = [r["scores"][0] for r in rows if r["answerable"] and r["scores"]]
un = [r["scores"][0] for r in rows if not r["answerable"] and r["scores"]]
print("answerable top: n=%d min=%.3f p25=%.3f med=%.3f" % (
    len(ans), min(ans), sorted(ans)[len(ans)//4], sorted(ans)[len(ans)//2]))
print("unanswerable top: n=%d min=%.3f med=%.3f max=%.3f" % (
    len(un), min(un), sorted(un)[len(un)//2], max(un)))
print("unanswerable tops:", sorted(round(x, 3) for x in un))

print("\n== 不可答却有结果：命中的有信息量词 ==")
for r in rows:
    if r["answerable"] or not r["n"]:
        continue
    terms = _informative_terms(r["query"])
    matched = [t for t in terms if any(t in p for p in r["preview"])]
    print("%-4s %-12s n=%d terms=%s matched=%s" % (r["id"], r["query"], r["n"], terms[:8], matched[:6]))

print("\n== 注入块内两两 Jaccard 分布（仅 n>=2） ==")
vals = []
for r in rows:
    toks = [set(_recall_tokens(p)) for p in r["preview"]]
    for i in range(len(toks)):
        for j in range(i + 1, len(toks)):
            if toks[i] and toks[j]:
                vals.append((_jaccard(toks[i], toks[j]), r["id"], r["query"]))
vals.sort(reverse=True)
for v in vals[:15]:
    print("  %.2f %s %s" % v)
for t in (0.35, 0.45, 0.5, 0.6):
    hit_q = {v[1] for v in vals if v[0] >= t}
    print("阈值 %.2f: 涉及 %d 条查询 / %d 对" % (t, len(hit_q), sum(1 for v in vals if v[0] >= t)))
