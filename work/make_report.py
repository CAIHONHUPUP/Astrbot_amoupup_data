# -*- coding: utf-8 -*-
"""把评估 JSON 变成报告用的 Markdown 表与失败清单。"""
import json, os, sys

path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "report_final.json")
rep = json.load(open(path, encoding="utf-8"))
arms = rep["arms"]
order = [a for a in ("like", "kw", "before", "current") if a in arms]
m = {a: arms[a]["metrics"] for a in order}

print("## 1. 总体指标\n")
print("| 指标 | " + " | ".join(order) + " |")
print("|---" * (len(order) + 1) + "|")
rows = [
    ("命中率 hit@k", "hit_rate", "%.3f"),
    ("precision@6", "precision_at_k", "%.3f"),
    ("precision@返回条数", "precision_returned", "%.3f"),
    ("MRR", "mrr", "%.3f"),
    ("空召回率", "empty_rate", "%.3f"),
    ("可答查询空召回率", "empty_rate_answerable", "%.3f"),
    ("无答案查询正确弃权率", "abstain_accuracy", "%.3f"),
    ("无答案查询噪声率", "noise_rate_unanswerable", "%.3f"),
    ("平均注入条数", "avg_items", "%.2f"),
    ("平均注入字数(原样)", "avg_inject_chars", "%.0f"),
    ("平均注入字数(120字截断)", "avg_inject_chars_capped", "%.0f"),
    ("近重复注入占比", "dup_inject_rate", "%.3f"),
    ("语义向量使用率", "vec_used_rate", "%.3f"),
    ("平均耗时 ms", "avg_ms", "%.0f"),
]
for label, key, fmt in rows:
    print("| %s | %s |" % (label, " | ".join(fmt % m[a][key] for a in order)))

print("\n## 2. 分类（before -> current）\n")
print("| 类别 | 查询数 | 命中率 | p@6 | 空召回 | 注入字数 |")
print("|---|---|---|---|---|---|")
cats = list(arms["current"]["by_cat"].keys())
for cat in cats:
    b = arms["before"]["by_cat"][cat]
    c = arms["current"]["by_cat"][cat]
    print("| %s | %d | %.2f -> %.2f | %.3f -> %.3f | %.2f -> %.2f | %.0f -> %.0f |" % (
        cat, c["queries"], b["hit_rate"], c["hit_rate"], b["precision_at_k"], c["precision_at_k"],
        b["empty_rate"], c["empty_rate"], b["avg_inject_chars"], c["avg_inject_chars"]))

cur = {r["id"]: r for r in arms["current"]["queries"]}
bef = {r["id"]: r for r in arms["before"]["queries"]}

print("\n## 3. 修复带来的逐条变化\n")
print("| id | 查询 | before | current |")
print("|---|---|---|---|")
for qid in cur:
    a, b = bef[qid], cur[qid]
    if a["ids"] == b["ids"]:
        continue
    print("| %s | %s | n=%d p=%.2f hit=%s | n=%d p=%.2f hit=%s |" % (
        qid, b["query"], a["n"], a["precision_at_k"], a["hit"], b["n"], b["precision_at_k"], b["hit"]))

print("\n## 4. 当前仍未命中（可答）\n")
for r in cur.values():
    if r["answerable"] and not r["hit"]:
        print("- %s [%s] %s：n=%d，返回 kinds=%s，预览=%s" % (
            r["id"], r["cat"], r["query"], r["n"], r["kinds"][:3],
            " / ".join(r["preview"][:1])[:60]))

print("\n## 5. 无答案却给了记忆（噪声）\n")
for r in cur.values():
    if not r["answerable"] and r["n"]:
        print("- %s %s：n=%d top=%s kinds=%s matched=%s" % (
            r["id"], r["query"], r["n"], r["scores"][0], r["kinds"][:3], r["matched"][:3]))

print("\n## 6. 注入最长的 8 条\n")
for r in sorted(cur.values(), key=lambda x: -x["inject_chars"])[:8]:
    print("- %s %s：inject=%d（截断后 %d）n=%d kinds=%s" % (
        r["id"], r["query"], r["inject_chars"], r["inject_chars_capped"], r["n"], r["kinds"][:4]))

print("\n## 7. 语义层（kw vs current，同一套证据词）\n")
kw = {r["id"]: r for r in arms["kw"]["queries"]}
diff = [qid for qid in cur if kw[qid]["ids"] != cur[qid]["ids"]]
print("排序/条数不同的查询数：%d / %d" % (len(diff), len(cur)))
for qid in diff[:8]:
    print("- %s %s：kw=%s current=%s" % (qid, cur[qid]["query"], kw[qid]["ids"], cur[qid]["ids"]))

print("\n## 8. 近重复对（before 臂，jaccard 明细）\n")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests"))
import recall_common as rc
rc.install_stub()
from utils.text_process import _recall_tokens, _jaccard
for r in arms["before"]["queries"]:
    toks = [set(_recall_tokens(p)) for p in r["preview"]]
    for i in range(len(toks)):
        for j in range(i + 1, len(toks)):
            jac = _jaccard(toks[i], toks[j])
            if jac >= 0.6:
                print("- %s %s：%.2f" % (r["id"], r["query"], jac))
