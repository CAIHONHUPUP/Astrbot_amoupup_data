# -*- coding: utf-8 -*-
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tests"))
import astrbot_stub; astrbot_stub.install()
from services import entity_graph
for text in ("小周送了我一条「围巾」", "小周最近换了工作", "围巾"):
    names = entity_graph.extract_entities(text)
    print([n.encode("unicode_escape").decode() for n in names])
