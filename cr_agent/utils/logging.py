"""
统一日志脱敏。

所有 PR 复用同一个过滤器,禁止各自实现 token / api_key / git_token 的 mask。
依赖方向单一:任何想写日志的模块都通过 get_logger() 拿到已挂载 RedactionFilter 的
logger,或直接调用 redact() 处理纯文本。
"""

from __future__ import annotations

import logging
import re

# 脱敏占位符。负向单测会断言日志里出现它而不是明文密钥。
REDACTED = "***REDACTED***"

# key=value / key: value 形式的敏感字段。只替换值,保留键名,方便排查
# “是否配置了 token”这类问题,同时绝不泄露明文。
_KEY_VALUE_PATTERN = re.compile(
    r"(?i)\b(token|api[_-]?key|apikey|git_token|authorization|password|passwd|secret)"
    r"(\"?\s*[=:]\s*\"?)"
    r"([^\s\"',;}]+)"
)

# HTTP Authorization: Bearer <token> 形式。
_BEARER_PATTERN = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._\-]+)")


def redact(text: str) -> str:
    """对一段文本做脱敏,供日志和需要打印用户态字符串的地方复用。"""
    redacted = _KEY_VALUE_PATTERN.sub(rf"\1\2{REDACTED}", text)
    redacted = _BEARER_PATTERN.sub(rf"\1{REDACTED}", redacted)
    return redacted


class RedactionFilter(logging.Filter):
    """
    把 token / api_key / git_token 等敏感值从日志记录中抹掉的过滤器。

    在 filter() 阶段先把 msg 与 args 渲染成最终字符串再脱敏,避免 args 里的
    明文绕过正则。脱敏后清空 args,保证下游 Formatter 不会再次插值出明文。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            # 渲染失败时回退到原始 msg 字符串,仍然脱敏,绝不放行明文。
            rendered = str(record.msg)
        record.msg = redact(rendered)
        record.args = ()
        return True


def install_redaction_filter(logger: logging.Logger) -> logging.Logger:
    """幂等地给 logger 挂上 RedactionFilter。"""
    if not any(isinstance(f, RedactionFilter) for f in logger.filters):
        logger.addFilter(RedactionFilter())
    return logger


def get_logger(name: str) -> logging.Logger:
    """返回已挂载脱敏过滤器的 logger。"""
    return install_redaction_filter(logging.getLogger(name))
