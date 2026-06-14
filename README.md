# trajery — Delivery Log → Teich Codex Trace

将 distillXC **delivery 原始日志**（`*.tar.gz` / `*.jsonl`）清洗、筛选，并导出 **Teich 合规的 Codex Agent Trace**。

---

## 快速开始

```bash
cd powverse/trajery
pip install -e /path/to/teich    # trace 校验与 training rows 导出需要

python delivery_to_teich.py <input_dir> [<output_dir>]
```

- 默认输出目录：`<input_dir>/output`
- 默认报表：`<output_dir>/report.json` 与 `<output_dir>/report.md`
- 要求：Python 3.10+；核心流水线仅需标准库；`teich` 默认必须安装（调试用 `--skip-teich-validate` 可跳过校验）

---

## 文档导航

| 读者 | 文档 | 内容 |
|------|------|------|
| **数据 / 运维**（跑批、验收） | [USER_GUIDE.md](USER_GUIDE.md) | 流程、CLI 选项、输出目录、报表字段、R1–R7、验收清单、FAQ |
| **开发 / 维护**（改规则、跑 CI） | [MAINTAINER.md](MAINTAINER.md) | 代码布局、规则修改、测试、已知限制、文档维护映射 |

### 常用跳转

| 主题 | 位置 |
|------|------|
| 命令行选项 | [USER_GUIDE §4](USER_GUIDE.md#4-命令行用法) |
| 输出目录与报表 | [USER_GUIDE §6](USER_GUIDE.md#6-输出与报表) |
| incomplete / invalid 含义 | [USER_GUIDE §6.1](USER_GUIDE.md#61-输出目录) · [FAQ](USER_GUIDE.md#10-常见问题) |
| R1–R7 筛选规则 | [USER_GUIDE §7](USER_GUIDE.md#7-七条筛选规则r1r7) |
| 退出码 | [USER_GUIDE §11](USER_GUIDE.md#11-退出码) |
| 改规则后如何验证 | [MAINTAINER §修改规则流程](MAINTAINER.md#修改规则流程) |

---

## 常用命令

```bash
# 小样本试跑
python delivery_to_teich.py ./fixtures/data --limit-records 5

# 生产跑批（并行 scan + 清空上次输出）
python delivery_to_teich.py <input_dir> <output_dir> --clean-output --workers 8

# CI 验收：无有效 trace 时退出 1
python delivery_to_teich.py <input_dir> --strict-empty

# 多平台原始 API 日志筛选（*.json，非 delivery 流水线）
python filter_traj_multi_plat.py <input_dir> [<output_dir>]
```

---

## 测试

```bash
python -m unittest discover -s tests -v
```

测试样本在 `fixtures/data/`；重新生成：`python fixtures/build_fixtures.py`。
