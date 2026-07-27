#!/usr/bin/env python
# coding=utf-8
"""
通信模块

提供WebSocket的统一接口，支持通过配置选择通信协议。
包含通信抽象层基类和通信客户端工厂。
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional
from unilabos.config.config import BasicConfig
from unilabos.runtime.profile_composition import build_runtime_drivers
from unilabos.runtime.profile_loader import (
    discover_driver_catalog,
    load_profiles,
)
from unilabos.utils import logger

if TYPE_CHECKING:
    from unilabos.resources.resource_tracker import ResourceTreeSet


class BaseCommunicationClient(ABC):
    """
    通信客户端抽象基类

    定义了所有通信客户端（WebSocket等）需要实现的接口。
    """

    def __init__(self):
        self.is_disabled = True
        self.client_id = ""

    @abstractmethod
    def start(self) -> None:
        """
        启动通信客户端连接
        """
        pass

    @abstractmethod
    def stop(self) -> None:
        """
        停止通信客户端连接
        """
        pass

    @abstractmethod
    def publish_device_status(self, device_status: dict, device_id: str, property_name: str) -> None:
        """
        发布设备状态信息

        Args:
            device_status: 设备状态字典
            device_id: 设备ID
            property_name: 属性名称
        """
        pass

    @abstractmethod
    def publish_job_status(
        self, feedback_data: dict, job_id: str, status: str, return_info: Optional[dict] = None
    ) -> None:
        """
        发布作业状态信息

        Args:
            feedback_data: 反馈数据
            job_id: 作业ID
            status: 作业状态
            return_info: 返回信息
        """
        pass

    @abstractmethod
    def send_ping(self, ping_id: str, timestamp: float) -> None:
        """
        发送ping消息

        Args:
            ping_id: ping ID
            timestamp: 时间戳
        """
        pass

    def publish_action_lock(self, device_id: str, action_name: str, free: bool) -> None:
        """
        主动上报单个 device+action 的锁(可用性)状态(默认空实现)

        Args:
            device_id: 设备ID
            action_name: 动作名称
            free: 是否空闲(True 空闲, False 占用)
        """
        pass

    def publish_action_locks(self, locks: list) -> None:
        """
        批量主动上报 device+action 的锁(可用性)状态(默认空实现)

        Args:
            locks: [{"device_id": str, "action_name": str, "free": bool}, ...]
        """
        pass

    def setup_pong_subscription(self) -> None:
        """
        设置pong消息订阅（可选实现）
        """
        pass

    def bind_material_state(
        self,
        resources: "ResourceTreeSet",
        *,
        source_id: str = "os-current",
    ) -> None:
        """绑定 OS 当前内存物料树。

        非本地 schedule transport 可以保持默认空实现；实现不得复制或接管
        ResourceTreeSet 的写权威。
        """

        del resources, source_id

    @property
    def is_connected(self) -> bool:
        """
        检查是否已连接

        Returns:
            是否已连接
        """
        return not self.is_disabled


class CommunicationClientFactory:
    """
    通信客户端工厂类

    根据配置文件中的通信协议设置创建相应的客户端实例。
    """

    _client_cache: Optional[BaseCommunicationClient] = None

    @classmethod
    def create_client(cls, protocol: Optional[str] = None) -> BaseCommunicationClient:
        """
        创建通信客户端实例

        Args:
            protocol: 指定的协议类型，如果为None则使用配置文件中的设置

        Returns:
            通信客户端实例

        Raises:
            ValueError: 当协议类型不支持时
        """
        if protocol is None:
            protocol = BasicConfig.communication_protocol

        protocol = protocol.lower()

        if protocol == "websocket":
            return cls._create_websocket_client()
        else:
            logger.error(f"[CommunicationFactory] Unsupported protocol: {protocol}")
            logger.warning("[CommunicationFactory] Falling back to WebSocket")
            return cls._create_websocket_client()

    @classmethod
    def get_client(cls, protocol: Optional[str] = None) -> BaseCommunicationClient:
        """
        获取通信客户端实例（单例模式）

        Args:
            protocol: 指定的协议类型，如果为None则使用配置文件中的设置

        Returns:
            通信客户端实例
        """
        if cls._client_cache is None:
            cls._client_cache = cls.create_client(protocol)
            logger.trace(f"[CommunicationFactory] Created {type(cls._client_cache).__name__} client")

        return cls._client_cache

    @classmethod
    def _create_websocket_client(cls) -> BaseCommunicationClient:
        """创建WebSocket客户端"""
        try:
            from unilabos.app.ws_client import WebSocketClient

            profile_paths = list(
                getattr(BasicConfig, "runtime_profile_paths", []) or []
            )
            if not profile_paths:
                return WebSocketClient()
            connection_resolver = getattr(
                BasicConfig,
                "runtime_connection_resolver",
                None,
            )
            if connection_resolver is None:
                connections = getattr(BasicConfig, "runtime_connections", {})
                if not isinstance(connections, dict):
                    raise ValueError("runtime_connections must be a mapping")
                if not connections:
                    raise ValueError(
                        "runtime_connection_resolver or runtime_connections is "
                        "required when Profiles are configured"
                    )

                def connection_resolver(connection_ref: str):
                    return connections.get(connection_ref)
            driver_catalog = discover_driver_catalog()
            profiles = load_profiles(
                profile_paths,
                driver_catalog=driver_catalog,
            )
            runtime_drivers = build_runtime_drivers(
                profiles,
                driver_catalog,
                connection_resolver,
            )
            return WebSocketClient(runtime_drivers=runtime_drivers)
        except Exception as e:
            logger.error(f"[CommunicationFactory] Failed to create WebSocket client: {str(e)}")
            raise

    @classmethod
    def reset_client(cls):
        """重置客户端缓存（用于测试或重新配置）"""
        if cls._client_cache:
            try:
                cls._client_cache.stop()
            except Exception as e:
                logger.warning(f"[CommunicationFactory] Error stopping old client: {str(e)}")

        cls._client_cache = None
        logger.info("[CommunicationFactory] Client cache reset")

    @classmethod
    def get_supported_protocols(cls) -> list[str]:
        """
        获取支持的协议列表

        Returns:
            支持的协议列表
        """
        return ["websocket"]


def get_communication_client(protocol: Optional[str] = None) -> BaseCommunicationClient:
    """
    获取通信客户端实例的便捷函数

    Args:
        protocol: 指定的协议类型，如果为None则使用配置文件中的设置

    Returns:
        通信客户端实例
    """
    return CommunicationClientFactory.get_client(protocol)
