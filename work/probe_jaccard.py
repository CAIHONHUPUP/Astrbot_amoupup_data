# -*- coding: utf-8 -*-
import sys, os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from utils.text_process import _recall_tokens, _jaccard
pairs = [
    ("他喜欢下雨天吃火锅", "他喜欢下雨天吃火锅，尤其是周末晚上"),
    ("他喜欢下雨天吃火锅", "他喜欢下雨天吃火锅和烧烤"),
    ("今天和老公去江边散步，风很大", "今天和老公去江边散步，风很大，还放了风筝"),
    ("主人喜欢周末晚上和老公一起去江边散步", "主人喜欢周末晚上和老公去江边散步聊天"),
    ("他喜欢下雨天吃火锅", "他喜欢下雨天去吃火锅"),
    ("他喜欢下雨天吃火锅", "他喜欢下雨天吃火锅啊"),
]
for a, b in pairs:
    print("%.3f  %s | %s" % (_jaccard(_recall_tokens(a), _recall_tokens(b)), a, b))
