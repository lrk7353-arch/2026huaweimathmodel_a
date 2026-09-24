# case_091官方整图单核恢复评估

使用原版wrapper和官方代码，plan=None、problem=0、timeout1200秒、单worker，独立缓存目录。原300秒timeout记录保留为original_failure.json，未改写原run。

| 图 | 状态 | Makespan | 耗时秒 | 缓存复用 | 与原失败键相同 |
|---|---|---:|---:|---|---|
| case_091 | success | 9186769 | 516.542 | False | True |

[真实评估记录](record.json) · [完整汇总](summary.json) · [保留的原失败](original_failure.json)

只在status=success、缓存键与结果完整性均验证后导入原运行缓存；失败不能替换为其他单核方案。
