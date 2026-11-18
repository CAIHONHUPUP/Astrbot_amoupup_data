# -*- coding: utf-8 -*-
"""对比生成中的 recall 结果与 tests/contracts/recall_golden.json。"""
import json, os, sys, shutil
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tests"))
import astrbot_stub
astrbot_stub.install()
from support import recall_corpus
GOLDEN = os.path.join(ROOT, "tests", "contracts", "recall_golden.json")
old = json.load(open(GOLDEN, encoding="utf-8"))
tmp = os.path.join(ROOT, "tests", ".golden-tmp")
shutil.rmtree(tmp, ignore_errors=True); os.makedirs(tmp)
new = recall_corpus.capture(tmp)
shutil.rmtree(tmp, ignore_errors=True)
for name in new:
    a, b = old.get(name), new[name]
    ai = [(i["id"], i["score"]) for i in a]
    bi = [(i["id"], i["score"]) for i in b]
    if ai == bi:
        continue
    print("== %s  n %d -> %d" % (name, len(a), len(b)))
    print("   ids  old=%s" % [x[0] for x in ai])
    print("   ids  new=%s" % [x[0] for x in bi])
    delta = sorted(abs(x[1] - y[1]) for x, y in zip(ai, bi)) if len(ai) == len(bi) else []
    if delta:
        print("   max|dscore|=%.4f" % delta[-1])
unchanged = [n for n in new if [(i["id"], i["score"]) for i in old.get(n, [])] == [(i["id"], i["score"]) for i in new[n]]]
print("unchanged scenarios:", unchanged)
if "--write" in sys.argv:
    text = json.dumps(new, ensure_ascii=False, indent=1, sort_keys=True)
    with open(GOLDEN, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text + "\n")
    print("golden 已重生成")
