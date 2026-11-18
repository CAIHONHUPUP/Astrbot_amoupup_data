# -*- coding: utf-8 -*-
"""跨周回放：时钟按周往前走，验证旧记忆按遗忘曲线沉底、重要度高的掉得慢。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sim_lib as sim
from sim_lib import FakePlugin, restore_clock, seed, set_clock

QUERIES = ["团子", "芒果", "吃螺蛳粉了吗"]


def main():
    plugin = FakePlugin()
    try:
        seed(plugin)
        for week in range(0, 9, 2):
            set_clock(sim.SIM_NOW + week * 7 * 86400)
            line = []
            for query in QUERIES:
                rows = plugin.recall(sim.SIM_BOT, sim.SIM_UID, query, limit=1)
                line.append("%s=%s" % (query, rows[0]["id"] if rows else "-"))
            print("week %d: %s" % (week, " ".join(line)))
        print("weeks: 每 2 周走一格；重要度 0.9 的过敏事实在最后一周仍能召回")
    finally:
        restore_clock()
        plugin.close()


if __name__ == "__main__":
    main()
