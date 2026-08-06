"""SZLab mock 本地启动配置；禁用云端 HostLink，只保留本地运行日志。"""


class BasicConfig:
    """覆盖 mock 验证需要的最小基础配置。"""

    log_level = "DEBUG"


class HostLinkConfig:
    """关闭 mock 本地启动时不需要的云端 Edge 长连接。"""

    enable = False
