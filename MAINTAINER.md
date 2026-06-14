# trajery 维护说明

面向**改规则、同步 vendor、跑 CI** 的维护者。操作跑批见 [USER_GUIDE.md](USER_GUIDE.md)。

## 与采购工具链的关系

[`filter_traj_multi_plat.py`](filter_traj_multi_plat.py) 是采购分包
[`traj_procurement_vendor_filter/multi_platform_traj_filter.py`](../traj_procurement_vendor_filter/multi_platform_traj_filter.py)
的 **vendor 副本**，供 [`delivery_to_teich.py`](delivery_to_teich.py) / `trajery.pipeline` import 使用。

以下逻辑必须与上游保持 **byte-compatible**（代码注释中有标注）：

- `canonicalize_trajectory` / `compute_session_id` ↔ `tools/traj_procurement/format/session_id.py`
- 归一化字段 ↔ `tools/traj_procurement/format/unified_format.py`

若仓库中存在 `tools/traj_procurement/`，优先在那里修改规则，再同步到本目录。

## 同步流程

1. 在 `tools/traj_procurement/filter/` 或 vendor 包中修改规则 / session_id 逻辑
2. 将变更复制到 `filter_traj_multi_plat.py`（或运行 vendor 包的 `build_single_file.py`）
3. 运行：

   ```bash
   cd powverse/trajery
   python run_tests_against_vendor.py
   python -m unittest discover -s tests -v
   ```

4. 若 vendor `examples/` 有新增 drop reason，在 `fixtures/build_fixtures.py` 中补充 delivery 信封样例

## 代码布局

| 路径 | 说明 |
|------|------|
| `delivery_to_teich.py` | CLI 薄入口 |
| `trajery/cli/teich.py` | 参数解析与 `main()` |
| `trajery/pipeline.py` | `process()`、`PipelineStats` |
| `trajery/parser/` | delivery 扫描、unwrap、SSE/chunk 解析 |
| `trajery/export/codex.py` | Codex JSONL 写出与 Teich 校验 |
| `filter_traj_multi_plat.py` | R1–R7 规则（vendor 副本，根目录便于同步） |
| `tests/` | 单元与端到端测试 |
| `fixtures/data/` | 合成 `.jsonl` / `.tar.gz` 样本 |

## 测试资产

- `fixtures/data/*.jsonl` — pass / 各 R1–R7 淘汰 / dedup / malformed
- `fixtures/data/delivery_multi_member.tar.gz` — 多成员 tar 告警
- `tests/test_filter_rules.py` — R1–R7 单规则
- `tests/test_teich_run.py` — 端到端、`--strict-empty`、`--clean-output`
- `run_tests_against_vendor.py` — 与 vendor filter 在 13 个 examples 上对比

重新生成 fixtures：`python fixtures/build_fixtures.py`

## 已知限制

- **tar.gz 单成员**：每个 archive 只读第一个 `.jsonl`；多成员写入 `report.json` 的 `tar_warnings`
- **dedup 内存缓冲**：超大目录需注意内存
- **Codex 时间戳**：导出时使用运行时 UTC，非 delivery 原始时间
- **输入形态**：主流水线仅处理 delivery 信封；`*.json` 原始日志用 `filter_traj_multi_plat.py`

## 重跑行为

- `output_dir` 扫描时自动排除，不会把上次 trace 当输入
- `traces/` / `incomplete/` / `invalid/` 按文件名覆盖；`dropped/` / `unwrapped/` 同路径覆盖
- `--clean-output` 跑批前清空上述五个子目录
