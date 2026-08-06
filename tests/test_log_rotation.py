"""验证 OS 诊断日志采用有界轮转，而非无限追加。"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

import pytest

from unilabos.utils.log import configure_comm_logger, configure_logger


@pytest.fixture(autouse=True)
def restore_default_loggers():
    """每个用例结束后恢复默认日志目录，避免临时处理器污染后续测试。"""

    yield
    configure_logger()
    configure_comm_logger()


def test_main_log_uses_configured_rotating_file_handler(tmp_path, monkeypatch) -> None:
    """主诊断日志应读取容量与保留份数配置，并创建轮转处理器。"""

    monkeypatch.setenv("UNILABOS_LOG_MAX_BYTES", "1024")
    monkeypatch.setenv("UNILABOS_LOG_BACKUP_COUNT", "2")

    configure_logger(working_dir=tmp_path)

    handlers = [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, RotatingFileHandler)
    ]
    assert len(handlers) == 1
    assert handlers[0].maxBytes == 1024
    assert handlers[0].backupCount == 2


def test_communication_log_uses_configured_rotating_file_handler(
    tmp_path,
    monkeypatch,
) -> None:
    """通信诊断日志应复用同一容量策略，同时保持独立日志文件。"""

    monkeypatch.setenv("UNILABOS_LOG_MAX_BYTES", "2048")
    monkeypatch.setenv("UNILABOS_LOG_BACKUP_COUNT", "3")

    configure_comm_logger(working_dir=tmp_path)

    handlers = [
        handler
        for handler in logging.getLogger("unilabos.comm").handlers
        if isinstance(handler, RotatingFileHandler)
    ]
    assert len(handlers) == 1
    assert handlers[0].maxBytes == 2048
    assert handlers[0].backupCount == 3
