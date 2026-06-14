"""命令行入口点 / Command-line entry points.

中文：cli 子包公开 API — ``main`` 为 ``delivery_to_teich.py`` 的 CLI 实现。

English: Re-exports the CLI ``main`` function for delivery_to_teich.

公开符号 / Public symbols:
- ``main`` — CLI 入口，返回退出码 / CLI entry, returns exit code
"""

from trajery.cli.delivery_to_teich import main

__all__ = ["main"]
