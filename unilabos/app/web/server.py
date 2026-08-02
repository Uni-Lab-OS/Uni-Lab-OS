"""
Web服务器模块

提供Web服务器功能，网页信息服务 + mqtt代替
"""

import webbrowser
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import Response

from unilabos.app.web.api import setup_api_routes
from unilabos.app.web.pages import setup_web_pages
from unilabos.config.config import BasicConfig, ObservabilityConfig
from unilabos.utils.fastapi.log_adapter import setup_fastapi_logging
from unilabos.utils.log import error, info

# 创建FastAPI应用
app = FastAPI(
    title="UniLab API",
    description="UniLab API Service",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

# 创建页面路由
pages = None
workflow_routes_mounted = False
observability_routes_mounted = False
observability_gateway = None

# noinspection PyTypeChecker
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Accept",
        "Last-Event-ID",
    ],
)


@app.middleware("http")
async def log_requests(request: Request, call_next) -> Response:
    """
    记录HTTP请求日志的中间件

    Args:
        request: 当前HTTP请求对象
        call_next: 下一个处理函数

    Returns:
        Response: HTTP响应对象
    """
    # # 打印请求信息
    # info(f"[Web] Request: {request.method} {request.url}", stack_level=1)
    # debug(f"[Web] Headers: {request.headers}", stack_level=1)
    #
    # # 使用日志模块记录请求体（如果需要）
    # body = await request.body()
    # if body:
    #     debug(f"[Web] Body: {body}", stack_level=1)

    # 调用下一个中间件或路由处理函数
    response = await call_next(request)

    # # 打印响应信息
    # info(f"[Web] Response status: {response.status_code}", stack_level=1)

    return response


def setup_server(
    *,
    registry_snapshot: Mapping[str, Any] | None = None,
    resource_registry_snapshot: Mapping[str, Any] | None = None,
    workflow_job_dispatcher: Any = None,
    device_identity_resolver: Callable[[str], str | None] | None = None,
    workflow_package_catalogs: tuple[Any, ...] = (),
) -> FastAPI:
    """
    设置服务器

    Returns:
        FastAPI: 配置好的FastAPI应用实例
    """
    global pages, workflow_routes_mounted
    global observability_routes_mounted, observability_gateway

    # 创建页面路由
    if pages is None:
        pages = app.router

    # 设置API路由
    setup_api_routes(app)

    # Electron 只通过 Uni-Lab-OS 上报和查询 trace；Phoenix 保持 loopback 私有实现。
    if not observability_routes_mounted:
        try:
            from unilabos.app.observability_api import install_observability_api
            from unilabos.observability.config import ObservabilitySettings
            from unilabos.observability.gateway import ObservabilityGateway

            observability_settings = ObservabilitySettings.from_runtime_config(
                BasicConfig,
                ObservabilityConfig,
            )
            observability_gateway = ObservabilityGateway(observability_settings)
            install_observability_api(app, observability_gateway)
            observability_routes_mounted = True
        except Exception as e:  # noqa: BLE001 - 可观测性不阻断设备运行
            error(f"[Web] 挂载 Phoenix trace 日志路由失败: {e!s}")

    # Backend-shaped Workflow authority 统一拥有本工作区的 workflow.db。
    if not workflow_routes_mounted and BasicConfig.working_dir:
        try:
            from unilabos.app.workflow_api import (
                install_composed_workflow_authoring_api,
            )
            from unilabos.workflow.catalog import CatalogAuthority
            from unilabos.workflow.composition import (
                compose_workflow_runtime,
                get_device_action_task_service,
            )

            authority = BasicConfig.workflow_graph_authority
            if not isinstance(authority, CatalogAuthority) or authority.kind != "local":
                raise TypeError("未配置有效的 Workflow Graph Authority")
            editable_package_roots = BasicConfig.workflow_editable_package_roots
            if not isinstance(editable_package_roots, tuple):
                raise TypeError("Workflow editable package roots 必须是 tuple")
            workflow_service = compose_workflow_runtime(
                BasicConfig.working_dir,
                authority=authority,
                editable_package_roots=editable_package_roots,
                registry_snapshot=registry_snapshot,
                resource_registry_snapshot=resource_registry_snapshot,
                workflow_job_dispatcher=workflow_job_dispatcher,
                device_identity_resolver=device_identity_resolver,
                workflow_package_catalogs=workflow_package_catalogs,
            )
            if workflow_service.compiler is None:
                raise RuntimeError("Workflow Authoring engine 未完成组合")
            template_catalog = getattr(
                workflow_service.compiler,
                "template_catalog",
                None,
            )
            catalog_authority = getattr(
                workflow_service.compiler,
                "catalog_authority",
                None,
            )
            from unilabos.app.scheduler.integration import get_edge_scheduler

            edge_scheduler = get_edge_scheduler()
            install_composed_workflow_authoring_api(
                app,
                workflow_service,
                workflow_service.compiler,
                template_catalog=template_catalog,
                catalog_authority=catalog_authority,
                device_action_tasks=get_device_action_task_service(),
                task_admission_coordinator=(
                    edge_scheduler.reconcile_task_admission
                    if edge_scheduler is not None
                    else None
                ),
            )
            workflow_routes_mounted = True
        except Exception as e:  # noqa: BLE001 - keep unrelated web surfaces alive
            error(f"[Web] 挂载 Workflow authority 路由失败: {e!s}")

    # Edge 调度器/仓储路由（--edge_scheduler 未启用时端点返回 503/不挂载）
    try:
        from unilabos.app.scheduler.api import create_scheduler_router
        from unilabos.app.scheduler.integration import (
            get_edge_backend,
            get_edge_scheduler,
            get_inventory_service,
        )

        app.include_router(
            create_scheduler_router(
                get_edge_scheduler,
                get_edge_backend,
                include_execution_shaped_workflow_routes=False,
            )
        )
        inventory_service = get_inventory_service()
        if inventory_service is not None:
            from unilabos.app.scheduler.inventory.api import (
                create_backend_material_router,
                create_router as create_inventory_router,
            )
            from unilabos.app.scheduler.inventory.layout import create_lab_router

            app.include_router(create_inventory_router(inventory_service))
            app.include_router(create_backend_material_router(inventory_service))
            app.include_router(create_lab_router(inventory_service))
    except Exception as e:  # noqa: BLE001 - 调度器路由挂载失败不影响主服务
        error(f"[Web] 挂载 Edge 调度器路由失败: {e!s}")

    # 设置页面路由
    try:
        setup_web_pages(pages)
        # info("[Web] 已加载Web UI模块")
    except ImportError as e:
        info(f"[Web] 未找到Web页面模块: {e!s}")
    except Exception as e:  # noqa: BLE001 - 页面装配错误不阻断 API
        error(f"[Web] 加载Web页面模块时出错: {e!s}")

    return app


def start_server(
    host: str = "0.0.0.0",
    port: int = 8002,
    open_browser: bool = True,
    *,
    registry_snapshot: Mapping[str, Any] | None = None,
    resource_registry_snapshot: Mapping[str, Any] | None = None,
    workflow_job_dispatcher: Any = None,
    device_identity_resolver: Callable[[str], str | None] | None = None,
    workflow_package_catalogs: tuple[Any, ...] = (),
) -> bool:
    """
    启动服务器

    Args:
        host: 服务器主机
        port: 服务器端口
        open_browser: 是否自动打开浏览器

    Returns:
        bool: True if restart was requested, False otherwise
    """
    import threading
    import time

    from uvicorn import Config, Server

    # 设置服务器
    setup_server(
        registry_snapshot=registry_snapshot,
        resource_registry_snapshot=resource_registry_snapshot,
        workflow_job_dispatcher=workflow_job_dispatcher,
        device_identity_resolver=device_identity_resolver,
        workflow_package_catalogs=workflow_package_catalogs,
    )

    # 配置日志
    log_config = setup_fastapi_logging()

    # 启动前打开浏览器
    if open_browser:
        # noinspection HttpUrlsUsage
        url = f"http://{host if host != '0.0.0.0' else 'localhost'}:{port}/status"
        info(f"[Web] 正在打开浏览器访问: {url}")
        try:
            webbrowser.open(url)
        except Exception as e:  # noqa: BLE001 - 浏览器启动失败不阻断服务
            error(f"[Web] 无法打开浏览器: {e!s}")

    # 启动服务器
    info(f"[Web] 启动FastAPI服务器: {host}:{port}")

    # 使用支持重启的模式
    config = Config(app=app, host=host, port=port, log_config=log_config)
    server = Server(config)

    # 启动服务器线程
    server_thread = threading.Thread(
        target=server.run, daemon=True, name="uvicorn_server"
    )
    server_thread.start()

    # info("[Web] Server started, monitoring for restart requests...")

    # 监控重启标志
    import unilabos.app.main as main_module

    while server_thread.is_alive():
        if (
            hasattr(main_module, "_restart_requested")
            and main_module._restart_requested
        ):
            restart_reason = getattr(main_module, "_restart_reason", "unknown")
            info(f"[Web] Restart requested via WebSocket, reason: {restart_reason}")
            main_module._restart_requested = False

            # 停止服务器
            server.should_exit = True
            server_thread.join(timeout=5)

            info("[Web] Server stopped, ready for restart")
            return True

        time.sleep(1)

    return False


# 当脚本直接运行时启动服务器
if __name__ == "__main__":
    start_server()
