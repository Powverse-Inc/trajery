# trajery — Delivery Log → Teich Codex Trace

将 distillXC **delivery 原始日志**（`*.tar.gz` / `*.jsonl`）清洗、筛选，并导出 **Teich 合规的 Codex Agent Trace**。

## 快速开始

```bash
cd powverse/trajery
pip install -e /path/to/teich    # trace 校验与 training rows 导出需要

python delivery_to_teich.py <input_dir> [<output_dir>]
```

默认输出目录为 `<input_dir>/output`，报表写入 `output/report.json`。

**要求**：Python 3.10+；核心流水线仅需标准库；`teich` 默认必须安装（调试用 `--skip-teich-validate` 可跳过校验）。

## 常用命令

```bash
# 小样本试跑
python delivery_to_teich.py ./fixtures/data --limit-records 5

# 重复跑批前清空上次输出
python delivery_to_teich.py <input_dir> <output_dir> --clean-output

# CI 验收：无有效 trace 时退出 1
python delivery_to_teich.py <input_dir> --strict-empty

# 多平台原始 API 日志筛选（*.json，非 delivery 流水线）
python filter_traj_multi_plat.py <input_dir> [<output_dir>]
```

## 文档

| 文档 | 内容 |
|------|------|
| [USER_GUIDE.md](USER_GUIDE.md) | 命令行选项、输出目录、报表字段、R1–R7 规则、验收与 FAQ |
| [MAINTAINER.md](MAINTAINER.md) | 规则同步、代码布局、测试与已知限制 |

退出码见 [USER_GUIDE.md — 退出码](USER_GUIDE.md#11-退出码)。

## 测试

```bash
python -m unittest discover -s tests -v
python run_tests_against_vendor.py
```

测试样本在 `fixtures/data/`；重新生成：`python fixtures/build_fixtures.py`。
