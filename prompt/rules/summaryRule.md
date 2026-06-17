# 汇总评级规则

## 输入约束

- 每条候选问题必须包含：`dimension`、`score`（0-100）
- `score` 必须为整数；超出范围按边界截断（小于 0 记为 0，大于 100 记为 100）

## 分维度评级映射

### A 组：Business、Security

- Critical：score >= 70
- Major：60 <= score < 70
- 丢弃：score < 60

### B 组：Performance、Dependency

- Critical：score >= 90
- Major：80 <= score < 90
- Minor：70 <= score < 80
- 丢弃：score < 70

### C 组：Maintainability、Testing、Error Handling

- Critical：score >= 95
- Major：80 <= score < 95
- Minor：70 <= score < 80
- 丢弃：score < 70

### D 组：Consistency、Readability、Documentation

- Major：score >= 90
- Minor：80 <= score < 90
- 丢弃：score < 80

## 去重与计数

- 相同问题判定键：`title + file_path + start_line + end_line`
- 命中相同判定键时：
  - 保留最高评级（Critical > Major > Minor）
  - `count` 累加
  - `locations` 去重合并

## 总体统计

- `critical_count`：最终 Critical 问题数
- `major_count`：最终 Major 问题数
- `minor_count`：最终 Minor 问题数
- `total_count`：`critical_count + major_count + minor_count`

## 输出顺序

- 问题列表按严重级别排序：Critical -> Major -> Minor
- 同级内按 `score` 降序排序；同分按 `file_path` 字典序排序
