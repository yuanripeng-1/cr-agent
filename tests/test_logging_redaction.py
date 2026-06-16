from __future__ import annotations

import logging

from cr_agent.utils.logging import (
    REDACTED,
    RedactionFilter,
    get_logger,
    install_run_log_handler,
    redact,
)


def test_redact_masks_common_secret_keys() -> None:
    for raw in (
        "git_token=ghp_secretvalue123",
        "api_key: sk-abc.def-XYZ",
        "token=aaaa.bbbb.cccc",
        'password="hunter2"',
        "Authorization: Bearer eyJhbGciOi.payload.sig",
    ):
        masked = redact(raw)
        assert REDACTED in masked


def test_redact_does_not_leak_token_plaintext() -> None:
    masked = redact("git_token=ghp_secretvalue123 done")
    assert "ghp_secretvalue123" not in masked
    # 键名保留,便于排查“是否配置了 token”。
    assert "git_token=" in masked


def test_filter_negative_case_no_plaintext_in_log_output(caplog) -> None:
    logger = logging.getLogger("cr_agent.tests.redaction")
    logger.addFilter(RedactionFilter())
    logger.setLevel(logging.INFO)

    with caplog.at_level(logging.INFO, logger="cr_agent.tests.redaction"):
        logger.info("calling git with git_token=%s and api_key=%s", "ghp_topsecret", "sk-leak")

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert "ghp_topsecret" not in rendered
    assert "sk-leak" not in rendered
    assert REDACTED in rendered


def test_get_logger_attaches_filter_idempotently() -> None:
    logger = get_logger("cr_agent.tests.idempotent")
    get_logger("cr_agent.tests.idempotent")
    assert sum(isinstance(f, RedactionFilter) for f in logger.filters) == 1


def test_install_run_log_handler_writes_cr_agent_logs_with_redaction(tmp_path) -> None:
    log_path = install_run_log_handler(tmp_path)
    logger = get_logger("cr_agent.tests.run_log")

    logger.info("MODEL_CALL_START request_id=req-1 api_key=%s", "sk-runlog-secret")

    content = log_path.read_text(encoding="utf-8")
    assert "MODEL_CALL_START request_id=req-1" in content
    assert "sk-runlog-secret" not in content
    assert REDACTED in content
