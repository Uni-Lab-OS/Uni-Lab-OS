"""软件包命令行（Package CLI）的稳定错误边界。"""


class PackageCLIError(RuntimeError):
    """表示软件包子命令可安全呈现给用户的预期错误。"""


__all__ = ["PackageCLIError"]
