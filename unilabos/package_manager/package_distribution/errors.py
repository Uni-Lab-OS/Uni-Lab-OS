"""包分发（Package Distribution）使用的稳定错误边界。"""

from __future__ import annotations

from typing import Any


class PackageCLIError(RuntimeError):
    """表示软件包子命令可安全呈现给用户的预期错误。"""


class PackageBuildError(RuntimeError):
    """表示软件包不能构建为经过自审计的标准 wheel。"""


class PackageTransferError(PackageCLIError):
    """表示上传或下载可用稳定机器码安全呈现的预期失败。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        """固定安全错误合同。

        参数：``code`` 是稳定机器码；``message`` 是不含凭据和签名地址的说明；
        ``retryable`` 表示原请求是否可安全重试；``details`` 只允许保存脱敏上下文。
        返回：无。
        异常：错误码或消息为空时抛出 ``ValueError``，禁止输出无身份失败。
        """

        if not isinstance(code, str) or not code.strip():
            raise ValueError("软件包传输错误码不能为空")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("软件包传输错误消息不能为空")
        self.code = code.strip()
        self.retryable = bool(retryable)
        self.details = dict(details or {})
        super().__init__(message.strip())

    def to_command_dict(self, *, command: str, environment: str) -> dict[str, Any]:
        """生成命令行失败 JSON。

        参数：``command`` 是 ``package.upload`` 或 ``package.download``；
        ``environment`` 是本次固定的环境标签。
        返回：不含远端响应正文、凭据或签名 URL 的新字典。
        异常：无；构造阶段已验证错误身份。
        """

        result: dict[str, Any] = {
            "schema_version": "unilab-package-command/v1",
            "command": command,
            "environment": environment,
            "status": "failed",
            "error": {
                "code": self.code,
                "message": str(self),
                "retryable": self.retryable,
            },
        }
        if self.details:
            result["error"]["details"] = dict(self.details)
        return result


__all__ = ["PackageBuildError", "PackageCLIError", "PackageTransferError"]
