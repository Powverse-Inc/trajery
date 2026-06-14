"""delivery 日志解析与 response 解码 / Delivery log parsing and response decoding.

中文：parser 子包公开 API — 扫描 delivery 文件、解包信封、解析 response 字符串。

English: Re-exports parser-layer functions for scan, unwrap, and response parsing.

公开符号 / Public symbols:
- ``iter_delivery_records`` — 主扫描迭代器 / main scan iterator
- ``iter_delivery_sources`` — 文件级扫描 / file-level discovery
- ``unwrap_delivery_record`` — L0→L1 解包 / envelope unwrap
- ``classify_unwrap_failure`` — unwrap 失败预判 / pre-flight unwrap check
- ``session_key_from_record`` — trace 命名键 / trace filename stem
- ``inspect_tar_jsonl_members`` — tar 成员元数据 / tar member metadata
- ``parse_delivery_response`` — response 字符串解析 / response string parser
"""

from trajery.parser.delivery import (
    classify_unwrap_failure,
    inspect_tar_jsonl_members,
    iter_delivery_records,
    iter_delivery_sources,
    session_key_from_record,
    unwrap_delivery_record,
)
from trajery.parser.response_sse import parse_delivery_response

__all__ = [
    "classify_unwrap_failure",
    "inspect_tar_jsonl_members",
    "iter_delivery_records",
    "iter_delivery_sources",
    "parse_delivery_response",
    "session_key_from_record",
    "unwrap_delivery_record",
]
