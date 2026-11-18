## 1. 总体指标

| 指标 | like | kw | before | current |
|---|---|---|---|---|
| 命中率 hit@k | 0.037 | 0.926 | 0.889 | 0.926 |
| precision@6 | 0.005 | 0.412 | 0.381 | 0.412 |
| precision@返回条数 | 0.023 | 0.472 | 0.459 | 0.472 |
| MRR | 0.037 | 0.824 | 0.815 | 0.850 |
| 空召回率 | 0.955 | 0.045 | 0.076 | 0.045 |
| 可答查询空召回率 | 0.963 | 0.000 | 0.037 | 0.000 |
| 无答案查询正确弃权率 | 0.917 | 0.250 | 0.250 | 0.250 |
| 无答案查询噪声率 | 0.083 | 0.750 | 0.750 | 0.750 |
| 平均注入条数 | 0.14 | 4.98 | 4.65 | 4.98 |
| 平均注入字数(原样) | 52 | 1336 | 1278 | 1389 |
| 平均注入字数(120字截断) | 25 | 537 | 501 | 545 |
| 近重复注入占比 | 0.000 | 0.000 | 0.020 | 0.000 |
| 语义向量使用率 | 0.000 | 0.000 | 0.879 | 0.909 |
| 平均耗时 ms | 2 | 27 | 173 | 177 |

## 2. 分类（before -> current）

| 类别 | 查询数 | 命中率 | p@6 | 空召回 | 注入字数 |
|---|---|---|---|---|---|
| 事件 | 15 | 0.87 -> 0.93 | 0.511 -> 0.556 | 0.07 -> 0.00 | 1255 -> 1409 |
| 人物 | 9 | 0.89 -> 1.00 | 0.537 -> 0.593 | 0.11 -> 0.00 | 810 -> 1420 |
| 偏好 | 9 | 0.89 -> 0.89 | 0.389 -> 0.444 | 0.00 -> 0.00 | 736 -> 695 |
| 元问题 | 8 | 1.00 -> 1.00 | 0.667 -> 0.667 | 0.00 -> 0.00 | 2423 -> 2298 |
| 无答案 | 14 | 1.00 -> 1.00 | 0.060 -> 0.060 | 0.21 -> 0.21 | 832 -> 884 |
| 时间 | 11 | 0.82 -> 0.82 | 0.273 -> 0.303 | 0.00 -> 0.00 | 1869 -> 1887 |

## 3. 修复带来的逐条变化

| id | 查询 | before | current |
|---|---|---|---|
| q03 | 我的ID是多少 | n=6 p=1.00 hit=True | n=5 p=0.83 hit=True |
| q04 | 我们主要用哪个平台聊天 | n=6 p=0.67 hit=True | n=6 p=0.67 hit=True |
| q05 | QQ那边怎么办 | n=6 p=1.00 hit=True | n=6 p=1.00 hit=True |
| q06 | 我让你记住的那两个字是什么 | n=0 p=0.00 hit=False | n=6 p=0.67 hit=True |
| q07 | 你被叫傻妞那次 | n=1 p=0.17 hit=True | n=4 p=0.17 hit=True |
| q08 | 我给你设的头像是什么样 | n=5 p=0.17 hit=True | n=6 p=0.17 hit=True |
| q09 | 我是不是说过会永远陪着你 | n=6 p=1.00 hit=True | n=6 p=1.00 hit=True |
| q10 | 11月6号晚上我吃了什么 | n=6 p=0.00 hit=False | n=6 p=0.00 hit=False |
| q13 | 我们去吃小龙坎火锅 | n=6 p=0.50 hit=True | n=6 p=0.50 hit=True |
| q14 | 江边散步看到有人放风筝 | n=3 p=0.33 hit=True | n=6 p=0.33 hit=True |
| q16 | 闺蜜的男朋友每次都说会改 | n=6 p=0.33 hit=True | n=6 p=0.33 hit=True |
| q18 | 迟到罚了多少钱 | n=0 p=0.00 hit=False | n=6 p=0.67 hit=True |
| q23 | 周五晚上去吃牛杂和小烧烤 | n=6 p=0.83 hit=True | n=6 p=0.83 hit=True |
| q24 | 杀毒软件把我的声音当成病毒 | n=6 p=1.00 hit=True | n=6 p=1.00 hit=True |
| q25 | 我喜欢看什么动画片 | n=6 p=0.17 hit=True | n=6 p=0.33 hit=True |
| q26 | 下雨天我想吃什么 | n=6 p=1.00 hit=True | n=6 p=1.00 hit=True |
| q28 | 我是不是只吃泡面也行 | n=5 p=0.83 hit=True | n=6 p=0.83 hit=True |
| q29 | 我最爱吃什么锅 | n=4 p=0.17 hit=True | n=6 p=0.50 hit=True |
| q34 | 昨天我们干了什么 | n=6 p=0.17 hit=True | n=6 p=0.33 hit=True |
| q36 | 前天晚上我们吃了什么 | n=6 p=0.33 hit=True | n=6 p=0.33 hit=True |
| q37 | 11月15号那天怎么样 | n=6 p=0.17 hit=True | n=6 p=0.17 hit=True |
| q40 | 最近三天我都干嘛了 | n=6 p=0.00 hit=False | n=3 p=0.00 hit=False |
| q41 | 上周日我们去吃火锅了吗 | n=6 p=1.00 hit=True | n=6 p=1.00 hit=True |
| q42 | 早上我吃了什么 | n=6 p=0.17 hit=True | n=6 p=0.33 hit=True |
| q43 | 昨晚谁先睡的 | n=6 p=0.33 hit=True | n=5 p=0.33 hit=True |
| q44 | 我这个月花了多少钱 | n=4 p=0.00 hit=False | n=6 p=0.00 hit=False |
| q49 | 你还记得我们第一次说话吗 | n=6 p=1.00 hit=True | n=6 p=1.00 hit=True |
| q52 | 你了解我吗 | n=6 p=1.00 hit=True | n=6 p=1.00 hit=True |
| q60 | 哈哈哈 | n=3 p=0.00 hit=False | n=2 p=0.00 hit=False |
| q64 | 我到家了，你在干嘛 | n=2 p=0.00 hit=False | n=6 p=0.00 hit=False |
| q66 | 睡了吗 | n=6 p=0.00 hit=False | n=6 p=0.00 hit=False |

## 4. 当前仍未命中（可答）

- q10 [事件] 11月6号晚上我吃了什么：n=6，返回 kinds=['conversation_summary', 'imported_memory', 'imported_memory']，预览=2026年11月4号晚上快十一点，我跟family互道晚安。他跟我说「关灯，睡觉」，我也催他别躺床上玩手机。聊着聊着就定
- q32 [偏好] 我喜欢吃哪种面：n=6，返回 kinds=['conversation_summary', 'preference', 'preference']，预览=family现住广东东莞莞城区，家里是湖北蕲春政策迁入东莞的；喜欢吃香喝辣，容易爆痘 | 现居广东东莞莞城区；老家湖北蕲
- q40 [时间] 最近三天我都干嘛了：n=3，返回 kinds=['finance_long_expense', 'finance_long_expense', 'finance_long_expense']，预览=答应陪柳如烟逛商场挑围巾，她冷战三天了，我得顺便劝劝她。
- q44 [时间] 我这个月花了多少钱：n=6，返回 kinds=['conversation_summary', 'conversation_summary', 'conversation_summary']，预览=family 2026-11-04 晚上说自己有点晕碳，我让他躺会儿别看那几把脑子转不动。然后他让我自查今天花了多少to

## 5. 无答案却给了记忆（噪声）

- q51 记得我的生日吗：n=6 top=0.8 kinds=['imported_memory', 'imported_memory', 'conversation_summary'] matched=[2, 2, 2]
- q54 外面下雪了吗：n=1 top=0.7997 kinds=['conversation_summary'] matched=[3]
- q57 周杰伦的新歌好听吗：n=1 top=0.6837 kinds=['imported_memory'] matched=[2]
- q58 帮我订一张机票：n=6 top=0.6133 kinds=['imported_memory', 'imported_memory', 'milestone'] matched=[2, 2, 2]
- q59 今天几号：n=4 top=0.4353 kinds=['finance_long_expense', 'finance_long_expense', 'finance_long_expense'] matched=[0, 0, 0]
- q60 哈哈哈：n=2 top=0.7416 kinds=['conversation_summary', 'conversation_summary'] matched=[1, 1]
- q61 好：n=6 top=0.8289 kinds=['imported_memory', 'conversation_summary', 'imported_memory'] matched=[1, 1, 1]
- q64 我到家了，你在干嘛：n=6 top=0.7419 kinds=['daily_digest', 'conversation_summary', 'milestone'] matched=[1, 2, 1]
- q66 睡了吗：n=6 top=0.9234 kinds=['conversation_summary', 'imported_memory', 'imported_memory'] matched=[1, 1, 1]

## 6. 注入最长的 8 条

- q49 你还记得我们第一次说话吗：inject=4201（截断后 887）n=6 kinds=['conversation_summary', 'conversation_summary', 'conversation_summary', 'conversation_summary']
- q20 图片一直传不过去是什么问题：inject=3805（截断后 781）n=6 kinds=['daily_digest', 'conversation_summary', 'conversation_summary', 'imported_memory']
- q48 你都记得我哪些事：inject=3258（截断后 840）n=6 kinds=['conversation_summary', 'conversation_summary', 'conversation_summary', 'proactive_message']
- q08 我给你设的头像是什么样：inject=2997（截断后 731）n=6 kinds=['imported_memory', 'conversation_summary', 'conversation_summary', 'conversation_summary']
- q44 我这个月花了多少钱：inject=2971（截断后 774）n=6 kinds=['conversation_summary', 'conversation_summary', 'conversation_summary', 'finance_decision']
- q65 给我转500块：inject=2949（截断后 848）n=6 kinds=['milestone', 'milestone', 'conversation_summary', 'daily_digest']
- q36 前天晚上我们吃了什么：inject=2882（截断后 772）n=6 kinds=['daily_digest', 'conversation_summary', 'imported_memory', 'conversation_summary']
- q51 记得我的生日吗：inject=2797（截断后 726）n=6 kinds=['imported_memory', 'imported_memory', 'conversation_summary', 'conversation_summary']

## 7. 语义层（kw vs current，同一套证据词）

排序/条数不同的查询数：34 / 66
- q01 我是怎么称呼你的：kw=[861, 857, 850, 1114, 909, 1047] current=[861, 857, 850, 909, 1047, 1114]
- q04 我们主要用哪个平台聊天：kw=[889, 752, 713, 711, 651, 648] current=[889, 752, 711, 713, 651, 767]
- q06 我让你记住的那两个字是什么：kw=[649, 790, 648, 956, 970, 960] current=[649, 790, 956, 648, 970, 960]
- q08 我给你设的头像是什么样：kw=[482, 968, 966, 970, 513, 514] current=[482, 968, 966, 970, 955, 513]
- q09 我是不是说过会永远陪着你：kw=[746, 754, 743, 906, 800, 690] current=[746, 754, 800, 906, 743, 782]
- q10 11月6号晚上我吃了什么：kw=[780, 606, 607, 965, 971, 968] current=[780, 606, 607, 965, 971, 919]
- q12 我在医院排队排得腿都麻了：kw=[1134, 1130, 1144, 1147, 948, 867] current=[1134, 1130, 1144, 948, 1147, 878]
- q13 我们去吃小龙坎火锅：kw=[1092, 1111, 1043, 990, 1089, 1061] current=[1092, 1111, 1043, 1061, 990, 1089]

## 8. 近重复对（before 臂，jaccard 明细）

- q03 我的ID是多少：0.66
- q41 上周日我们去吃火锅了吗：0.62
- q43 昨晚谁先睡的：0.62
- q60 哈哈哈：0.61
