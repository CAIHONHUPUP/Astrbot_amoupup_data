# -*- coding: utf-8 -*-
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import recall_common as rc
rc.install_stub()
from utils.text_process import _informative_terms as old
from recall_eval import informative_terms_fixed as new
for q in ["迟到罚了多少钱", "我让你记住的那两个字是什么", "我想学钢琴", "周杰伦的新歌好听吗",
          "我这个月花了多少钱", "11月6号晚上我吃了什么", "昨晚谁先睡的", "睡了吗", "哈哈哈",
          "昨天我们干了什么", "外面下雪了吗", "帮我订一张机票", "好", "遇到她"]:
    o, n = old(q), new(q)
    print("%-14s old=%-38s new=%s" % (q, str(o[:6]), str(n[:6])))
