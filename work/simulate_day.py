# -*- coding: utf-8 -*-
"""一天的召回回放：固定语料 + 固定时钟，打印每条查询的命中与注入字数。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sim_lib as sim
from sim_lib import FakePlugin, restore_clock, seed, set_clock

QUERIES = ["螺蛳粉", "团子", "迟到罚了多少钱", "昨天我们干了什么", "我想学钢琴"]


def main():
    plugin = FakePlugin()
    set_clock(sim.SIM_NOW)
    try:
        seed(plugin)
        hits = 0
        for query in QUERIES:
            rows = plugin.recall(sim.SIM_BOT, sim.SIM_UID, query, limit=6)
            hits += 1 if rows else 0
            print("%-16s n=%d %s" % (query, len(rows),
                                     " / ".join(str(r["content"])[:24] for r in rows[:2])))
        print("day: %d/%d 条查询有召回" % (hits, len(QUERIES)))
    finally:
        restore_clock()
        plugin.close()


if __name__ == "__main__":
    main()
