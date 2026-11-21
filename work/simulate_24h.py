# -*- coding: utf-8 -*-
"""24 小时逐小时的召回回放：同一个库，时钟每小时走一格，看新鲜度/时间窗怎么变。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sim_lib as sim
from sim_lib import FakePlugin, restore_clock, seed, set_clock

QUERY = "昨天我们干了什么"


def main():
    plugin = FakePlugin()
    try:
        seed(plugin)
        changed = 0
        last = None
        for hour in range(24):
            set_clock(sim.SIM_NOW + hour * 3600)
            rows = plugin.recall(sim.SIM_BOT, sim.SIM_UID, QUERY, limit=3)
            ids = [int(row["id"]) for row in rows]
            if ids != last:
                changed += 1
                last = ids
            print("h=%02d n=%d ids=%s" % (hour, len(rows), ids))
        print("24h: 阵容变化 %d 次" % changed)
    finally:
        restore_clock()
        plugin.close()


if __name__ == "__main__":
    main()
