# 数据表初始化

本版通过 `src.models.base.Base.metadata.create_all` 创建首次运行所需的七张表。模型在 `src/models/workflow.py`。`python -m scripts.seed_demo` 按观测内容键补全演示样本，保留现有分析及签核。数据库端口只发布到本机。

## 周报版本化表结构

- `observations`：以 `(week, version)` 唯一约束保存每个交接周的各来源版本；`source_key` 仍为整行内容哈希。同一周可以并存多个版本，晚到的旧版本只新增行，不覆盖已入库的新版本。
- `ingest_batches`：按批次内容哈希幂等登记。同批重跑直接返回首次受理结果，不新增版本；同周同版出现不同数值时，业务层在同一事务内整批拒收，不写入任何记录。
- `analysis_runs.sample_versions`：冻结受理时每个交接周采用的来源版本号，与 `observation_ids` 一起构成可追溯快照；排队与计算都只读取冻结的观测 ID。

## 重建数据库

版本化字段为结构性变更，旧命名卷里的表不具备 `week`、`version`、`sample_versions` 等列。本地演示环境重建步骤：

```sh
docker compose down -v
docker compose up -d --build --wait
docker compose exec -T backend python -m scripts.seed_demo
```
