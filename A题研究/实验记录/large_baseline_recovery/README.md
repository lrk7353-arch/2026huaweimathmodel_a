# 大图官方单核恢复评估

仅复算072、076两张图；使用原版官方整图单核入口，plan=None、problem=0、每图1200秒上限、最多两并发。
原300秒timeout记录保存在original_failures中，未删除或改写。输出完全独立于正在运行的full_initial_v1。

| 图 | 状态 | 官方Makespan | 本次耗时秒 | 缓存复用 | 与原失败键相同 |
|---|---|---:|---:|---|---|
| case_072 | success | 23897828 | 493.329 | False | True |
| case_076 | success | 6185113 | 572.034 | False | True |

结果为success时才能由主任务按精确cache_key导入并续跑；timeout/runtime_error必须保留为未完成，不得替代成其他单核方案。

[完整汇总](summary.json) · [运行清单及原失败引用](manifest.json)
