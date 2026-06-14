# trajery 维护说明

> **读者**：改规则、同步 vendor、跑 CI、改流水线代码的维护者。  
> **跑批与验收**见 [USER_GUIDE.md](USER_GUIDE.md)；**快速入门**见 [README.md](README.md)。

---

## 目录

1. [与采购工具链的关系](#与采购工具链的关系)
2. [同步流程](#同步流程)
3. [代码布局](#代码布局)
4. [测试资产](#测试资产)
5. [已知限制](#已知限制)
6. [重跑行为](#重跑行为)
7. [文档维护映射](#文档维护映射)

---

## 与采购工具链的关系

[`filter_traj_multi_plat.py`](filter_traj_multi_plat.py) 是采购分包
[`traj_procurement_vendor_filter/multi_platform_traj_filter.py`](../traj_procurement_vendor_filter/multi_platform_traj_filter.py)
的 **vendor 副本**，供 [`delivery_to_teich.py`](delivery_to_teich.py) / `trajery.pipeline` import 使用。

以下逻辑必须与上游保持 **byte-compatible**（代码注释中有标注）：

- `canonicalize_trajectory` / `compute_session_id` ↔ `tools/traj_procurement/format/session_id.py`
- 归一化字段 ↔ `tools/traj_procurement/format/unified_format.py`

若仓库中存在 `tools/traj_procurement/`，优先在那里修改规则，再同步到本目录。

---

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
5. 若新增 CLI 选项、报表字段或输出目录，同步更新 [USER_GUIDE.md](USER_GUIDE.md) 与 [README.md](README.md)（见 [文档维护映射](#文档维护映射)）

---

## 代码布局

| 路径 | 说明 |
|------|------|
| `delivery_to_teich.py` | CLI 薄入口 |
| `trajery/cli/delivery_to_teich.py` | 参数解析、`main()`、写 `report.json` / `report.md` |
| `trajery/pipeline.py` | `process()`、`PipelineStats`、`TeichTraceResult` |
| `trajery/report.py` | `to_markdown_report()`，从 `to_report()` 数据生成 MD |
| `trajery/pipeline_scan.py` | 并行 scan worker（`--workers > 1`） |
| `trajery/parser/` | delivery 扫描、unwrap、SSE/chunk 解析 |
| `trajery/export/codex.py` | Codex JSONL 写出与 Teich 校验 |
| `filter_traj_multi_plat.py` | R1–R7 规则（vendor 副本，根目录便于同步） |
| `tests/` | 单元与端到端测试 |
| `fixtures/data/` | 合成 `.jsonl` / `.tar.gz` 样本 |

---

## 测试资产

| 资产 | 用途 |
|------|------|
| `fixtures/data/*.jsonl` | pass / 各 R1–R7 淘汰 / dedup / malformed |
| `fixtures/data/delivery_multi_member.tar.gz` | 多成员 tar 告警 |
| `tests/test_filter_rules.py` | R1–R7 单规则 |
| `tests/test_delivery_to_teich.py` | 端到端、报表、`--strict-empty`、`--clean-output`、并行 scan |
| `tests/test_teich_exporter.py` | Codex 导出与 Teich 校验 |
| `run_tests_against_vendor.py` | 与 vendor filter 在 13 个 examples 上对比 |

重新生成 fixtures：`python fixtures/build_fixtures.py`

---

## 已知限制

完整说明见 [USER_GUIDE §5](USER_GUIDE.md#5-输入限制)。维护时需知：

| 限制 | 影响范围 | 用户文档 |
|------|----------|----------|
| tar.gz 仅读第一个 `.jsonl` 成员 | `parser/delivery.py` | USER_GUIDE §5 |
| dedup 全量内存缓冲 | `pipeline.py` scan 阶段 | USER_GUIDE §5 |
| Codex 时间戳为导出时 UTC | `export/codex.py` | USER_GUIDE §5 |
| 主流水线仅处理 delivery 信封 | — | USER_GUIDE §4.4（`filter_traj_multi_plat.py` 处理 `*.json`） |
| R5 与 Teich `trace_is_complete` 两层标准 | `filter` + `export` | USER_GUIDE §6.1 · §7.2 · FAQ |

---

## 重跑行为

| 行为 | 说明 |
|------|------|
| `output_dir` 排除 | 扫描输入时自动排除，不会把上次 trace 当输入 |
| 同名覆盖 | `traces/` / `incomplete/` / `invalid/` 按文件名覆盖；`dropped/` / `unwrapped/` 同路径覆盖 |
| `--clean-output` | 跑批前清空五个输出子目录 |

用户向说明见 [USER_GUIDE FAQ](USER_GUIDE.md#10-常见问题)。

---

## 文档维护映射

仓库内 Markdown 仅三份，职责如下：

| 文件 | 职责 | 何时更新 |
|------|------|----------|
| [README.md](README.md) | 入口、快速开始、文档导航 | 新增顶层能力、改默认行为、改测试命令 |
| [USER_GUIDE.md](USER_GUIDE.md) | 操作手册（流程、CLI、输出、规则、验收、FAQ） | 改 CLI 选项、输出结构、报表字段、R1–R7、退出码 |
| [MAINTAINER.md](MAINTAINER.md) | 开发维护（同步、布局、测试、限制） | 改代码布局、vendor 同步、测试资产、架构限制 |

### 代码注释中的文档引用

改 USER_GUIDE 章节编号时，需同步检查：

| 引用位置 | 当前指向 |
|----------|----------|
| `trajery/pipeline.py` 模块头 | USER_GUIDE §2（流程）、§6.2（报表字段）、§6.4（指标关系） |
| `trajery/export/codex.py` | USER_GUIDE §5（时间戳） |
| `trajery/parser/delivery.py` | USER_GUIDE §5（tar 单成员） |
| `trajery/parser/response_sse.py` | USER_GUIDE §7.2（R5 / openai_responses） |
| `trajery/cli/delivery_to_teich.py` | USER_GUIDE §11（退出码） |
| `trajery/report.py` 附录 | USER_GUIDE §6–§10 |

### 跑批产出中的文档

`report.md` 由 `trajery/report.py` 生成，附录链接 USER_GUIDE。改报表结构时同步更新 `report.py` 与 USER_GUIDE §6。
