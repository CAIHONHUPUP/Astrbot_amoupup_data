# -*- coding: utf-8 -*-
"""分析评估报告：失败案例、噪声、注入长度、近重复。"""
import json, sys, os

path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "report_baseline.json")
rep = json.load(open(path, encoding="utf-8"))
arms = rep["arms"]
order = list(arms.keys())

print("== 指标总表 ==")
keys = ["hit_rate", "precision_at_k", "precision_at_k_answerable", "mrr", "empty_rate",
        "empty_rate_answerable", "abstain_accuracy", "noise_rate_unanswerable",
        "avg_items", "avg_inject_chars", "avg_content_chars", "dup_inject_rate", "vec_used_rate", "avg_ms"]
print("%-22s" % "metric" + "".join("%12s" % a for a in order))
for key in keys:
    print("%-22s" % key + "".join("%12.3f" % arms[a]["metrics"][key] for a in order))

print("\n== 分类命中率/精确率 (arm=%s) ==" % order[-1])
a = arms[order[-1]]["by_cat"]
print("%-8s %6s %8s %8s %8s %10s" % ("cat", "n", "hit", "p@k", "empty", "inject"))
for cat, m in a.items():
    print("%-8s %6d %8.2f %8.3f %8.2f %10.0f" % (cat, m["queries"], m["hit_rate"], m["precision_at_k"], m["empty_rate"], m["avg_inject_chars"]))

for arm in order:
    rows = arms[arm]["queries"]
    misses = [r for r in rows if r["answerable"] and not r["hit"]]
    print("\n== %s 未命中（answerable）%d 条 ==" % (arm, len(misses)))
    for r in misses:
        print("  %s [%s] %s | n=%d ids=%s kinds=%s | %s" % (
            r["id"], r["cat"], r["query"], r["n"], r["ids"][:6], r["kinds"][:4],
            " / ".join(r["preview"][:2])[:90]))
    noise = [r for r in rows if not r["answerable"] and r["n"]]
    print("== %s 无答案却给了记忆 %d 条 ==" % (arm, len(noise)))
    for r in noise:
        print("  %s %s | ids=%s | %s" % (r["id"], r["query"], r["ids"][:4], " / ".join(r["preview"][:2])[:80]))

cur = arms[order[-1]]["queries"]
print("\n== 注入最长 top8 (arm=%s) ==" % order[-1])
for r in sorted(cur, key=lambda x: -x["inject_chars"])[:8]:
    print("  %s %s inject=%d chars=%d n=%d kinds=%s" % (
        r["id"], r["query"], r["inject_chars"], r["content_chars"], r["n"], r["kinds"]))

print("\n== kw vs current 差异 ==")
kw = {r["id"]: r for r in arms["kw"]["queries"]}
for r in cur:
    if kw[r["id"]]["ids"] != r["ids"]:
        print("  %s %s kw=%s cur=%s" % (r["id"], r["query"], kw[r["id"]]["ids"], r["ids"]))
