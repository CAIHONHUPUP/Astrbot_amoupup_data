# -*- coding: utf-8 -*-
import json, sys
p = sys.argv[1]
d = json.load(open(p, encoding="utf-8"))
for arm, block in d["arms"].items():
    m = block["metrics"]
    print(arm, "overrides=", block.get("settings_overrides"),
          "| hit=%.2f p@k=%.3f empty=%.2f abstain=%.2f inject=%.0f items=%.1f vec=%.2f" % (
              m["hit_rate"], m["precision_at_k"], m["empty_rate"],
              m["abstain_accuracy"], m["avg_inject_chars"], m["avg_items"],
              m["vec_used_rate"]))
arm = "current"
rows = d["arms"][arm]["queries"]
rows.sort(key=lambda r: -float(r.get("inject_chars") or 0))
for r in rows[:4]:
    print("  inflate:", r["id"], r["cat"], "n=", r["n"], "chars=", r["inject_chars"],
          "answerable=", r.get("answerable"), "|", str(r.get("query"))[:30])
unans = [r for r in rows if not r.get("answerable")]
print("unanswerable with items:", sum(1 for r in unans if r["n"]), "/", len(unans))
