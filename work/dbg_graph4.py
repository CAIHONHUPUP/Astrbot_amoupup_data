# -*- coding: utf-8 -*-
import jieba.posseg as pseg
for text in ("小周送了我一条「围巾」", "小周最近换了工作", "围巾"):
    print([(w.encode("unicode_escape").decode(), f) for w, f in pseg.cut(text)])
