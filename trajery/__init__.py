"""trajery 包根：delivery → Teich Codex trace 工具集。
Delivery log → Teich Codex trace toolkit.

中文：
- 包级公开 API re-export，供 ``from trajery import process, PipelineStats`` 使用。
- ``process`` — 主流水线入口（scan → filter → dedup → export → validate）
- ``PipelineStats`` — 流水线计数器与 ``to_report()`` 报表序列化

English:
- Top-level public API for programmatic pipeline invocation.
"""

from trajery.pipeline import PipelineStats, process

__all__ = ["PipelineStats", "process"]
