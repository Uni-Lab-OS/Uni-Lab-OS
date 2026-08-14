"""
Web服务器模块

提供Web服务器功能，网页信息服务 + mqtt代替
"""

import webbrowser

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, Response

from unilabos.utils.fastapi.log_adapter import setup_fastapi_logging
from unilabos.utils.log import info, error
from unilabos.utils.tracing import install_http_tracing
from unilabos.app.web.api import setup_api_routes
from unilabos.app.web.pages import setup_web_pages
from unilabos.config.config import BasicConfig

# 创建FastAPI应用
app = FastAPI(
    title="UniLab API",
    description="UniLab API Service",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)
install_http_tracing(app)

# 创建页面路由
pages = None
workflow_routes_mounted = False
workflow_routes_mount_error: str | None = None
resource_contract_routes_mounted = False
workspace_authoring_routes_mounted = False
robot_commissioning_routes_mounted = False

_WORKFLOW_CONTRACT_PREFIXES = (
    "/api/v1/workflows",
    "/api/v1/workflow-tasks",
    "/api/v1/workflow-node-jobs",
    "/api/v1/workflow-node-templates",
    "/api/v1/device-action-runs",
    "/api/v1/events",
    "/api/v1/authoring",
)


def _is_workflow_contract_path(path: str) -> bool:
    """判断请求是否属于延后挂载的工作流合同。"""

    return any(
        path == prefix or path.startswith(f"{prefix}/")
        for prefix in _WORKFLOW_CONTRACT_PREFIXES
    )

# noinspection PyTypeChecker
app.add_middleware(
    CORSMiddleware,
    # Reflect every accepted Origin instead of combining the wildcard value
    # with credentialed Workbench requests, which browsers reject.
    allow_origins=[],
    allow_origin_regex=r".*",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Accept",
        "Last-Event-ID",
        "traceparent",
        "tracestate",
    ],
    expose_headers=["trace_id", "span_id"],
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


@app.middleware("http")
async def workflow_runtime_gate(request: Request, call_next) -> Response:
    """工作流合同未挂载时返回可重试 503，避免目录页把冷启动当成后端损坏。"""

    if (
        request.method != "OPTIONS"
        and _is_workflow_contract_path(request.url.path)
        and not workflow_routes_mounted
    ):
        if workflow_routes_mount_error:
            message = f"工作流目录装配失败: {workflow_routes_mount_error}"
            code = "workflow_runtime_unavailable"
            retryable = False
        else:
            message = "工作流目录仍在装配，请稍后重试"
            code = "workflow_runtime_mounting"
            retryable = True
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": code,
                    "message": message,
                    "retryable": retryable,
                }
            },
        )
    return await call_next(request)


def _mount_workflow_runtime() -> None:
    """装配本地工作流权威；失败时只关闭该合同，不回退第二套运行时。

    参数：无。返回：无。工作流源码固定点激活可能持续数分钟，调用方必须在
    HTTP 已经对外监听之后再执行本函数。异常：组合错误写入产品日志，不向外
    抛出，以免拖垮已启动的 FastAPI。
    """

    global workflow_routes_mounted, workflow_routes_mount_error
    if workflow_routes_mounted or not BasicConfig.working_dir:
        return
    info("[Web] 开始装配 Backend Workflow 合同")
    try:
        from unilabos.app.runtime_storage import get_runtime_storage_directory
        from unilabos.app.scheduler.integration import (
            get_edge_scheduler,
            get_inventory_service,
        )
        from unilabos.app.workflow_api import install_workflow_api
        from unilabos.workflow.composition import (
            compose_local_workflow_template_runtime,
            compose_workflow_runtime,
        )

        workflow_runtime_directory = (
            get_runtime_storage_directory() or BasicConfig.working_dir
        )

        # ``template_projection`` 只在本地调度与库存权威同时存在时建立；
        # Backend-controlled 模式不能在 OS 再创建第二个生产模板写权威。
        template_projection = None
        inventory_service = get_inventory_service()
        # ``edge_scheduler`` 是本地调度权威（Scheduler Authority）；只把同一
        # 已装配实例交给工作流组合根，禁止重新创建第二个调度器。
        edge_scheduler = get_edge_scheduler()
        # ``source_plan_arguments`` 只在工作区运行时传入预编译工作流
        # 源码（Workflow Source）计划，保持旧可编辑包组合接线兼容。
        source_plan_arguments = {}
        if BasicConfig.workflow_source_discovery_plan is not None:
            source_plan_arguments["editable_source_discovery_plan"] = (
                BasicConfig.workflow_source_discovery_plan
            )
        # 工作区（Workspace）由统一文件世代监视器拥有刷新；逐工作流源码
        # 监视器（Workflow Source Monitor）只保留给非工作区遗留入口。
        source_plan_arguments["start_source_monitor"] = (
            BasicConfig.workflow_source_discovery_plan is None
        )
        if inventory_service is not None and edge_scheduler is not None:
            from unilabos.registry.registry import lab_registry

            workflow_service, template_projection = (
                compose_local_workflow_template_runtime(
                    workflow_runtime_directory,
                    inventory_store=inventory_service.store,
                    registry=lab_registry,
                    scheduler=edge_scheduler,
                    editable_package_roots=(
                        BasicConfig.workflow_editable_package_roots
                    ),
                    **source_plan_arguments,
                )
            )
        else:
            workflow_service = compose_workflow_runtime(
                workflow_runtime_directory,
                editable_package_roots=(
                    BasicConfig.workflow_editable_package_roots
                ),
                **source_plan_arguments,
            )
        install_workflow_api(
            app,
            workflow_service,
            template_snapshot_provider=template_projection,
            authoring_transform=workflow_service.compiler,
        )
        workflow_routes_mounted = True
        workflow_routes_mount_error = None
        info("[Web] Backend Workflow 合同已挂载")
    except Exception as e:  # noqa: BLE001 - unrelated Edge routes remain available
        workflow_routes_mount_error = str(e)
        error(f"[Web] 挂载 Backend Workflow 合同失败: {str(e)}")


def setup_server() -> FastAPI:
    """装配当前产品配置允许的 Web 路由；工作流合同延后到 HTTP 监听之后。

    参数：无。返回：进程唯一 FastAPI 应用；重复调用复用已挂载路由。工作流
    源码（Workflow Source）授权形状或组合失败时关闭该合同路由，但不阻止无关
    Edge 路由继续装配，错误写入产品日志。
    异常：基础 FastAPI 路由装配错误原样传播。
    """
    global pages, resource_contract_routes_mounted
    global workspace_authoring_routes_mounted, robot_commissioning_routes_mounted

    # 创建页面路由
    if pages is None:
        pages = app.router

    # 设置API路由
    setup_api_routes(app)

    if not robot_commissioning_routes_mounted:
        from unilabos.app.robot_commissioning import (
            create_robot_commissioning_router,
            get_robot_commissioning_service,
        )

        app.include_router(
            create_robot_commissioning_router(get_robot_commissioning_service())
        )
        robot_commissioning_routes_mounted = True

    if (
        not workspace_authoring_routes_mounted
        and BasicConfig.workspace_package_mount_projection is not None
    ):
        from unilabos.app.workspace_authoring_api import (
            install_workspace_authoring_api,
        )

        install_workspace_authoring_api(
            app,
            BasicConfig.workspace_package_mount_projection,
        )
        workspace_authoring_routes_mounted = True

    # Edge 调度器与 Host 物料路由独立挂载；本地调度默认启用，无需正向开关。
    try:
        from unilabos.app.scheduler.api import create_scheduler_router
        from unilabos.app.scheduler.integration import (
            get_edge_backend,
            get_edge_scheduler,
            get_inventory_service,
            get_material_model_catalog,
            get_material_shapes,
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
            from unilabos.app.scheduler.inventory.backend_api import (
                install_backend_resource_api,
            )
            from unilabos.app.scheduler.inventory.backend_contract import (
                BackendResourceService,
            )
            from unilabos.app.scheduler.inventory.api import (
                create_legacy_material_router,
                create_router as create_inventory_router,
            )
            from unilabos.app.scheduler.inventory.layout import create_lab_router

            if not resource_contract_routes_mounted:
                install_backend_resource_api(
                    app,
                    BackendResourceService(inventory_service.store),
                    material_shapes=get_material_shapes(),
                    material_model_catalog=get_material_model_catalog(),
                )
                resource_contract_routes_mounted = True
            app.include_router(create_inventory_router(inventory_service))
            app.include_router(create_legacy_material_router(inventory_service))
            app.include_router(create_lab_router(inventory_service))
    except Exception as e:  # noqa: BLE001 - 调度器路由挂载失败不影响主服务
        error(f"[Web] 挂载 Edge 调度器路由失败: {str(e)}")

    # 设置页面路由
    try:
        setup_web_pages(pages)
        # info("[Web] 已加载Web UI模块")
    except ImportError as e:
        info(f"[Web] 未找到Web页面模块: {str(e)}")
    except Exception as e:
        error(f"[Web] 加载Web页面模块时出错: {str(e)}")

    return app


def start_server(host: str = "0.0.0.0", port: int = 8002, open_browser: bool = True) -> bool:
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

    # 先装配与工作流冷启动无关的路由，避免源码固定点挡住 HTTP。
    setup_server()

    # 配置日志
    log_config = setup_fastapi_logging()

    # 启动前打开浏览器
    if open_browser:
        # noinspection HttpUrlsUsage
        url = f"http://{host if host != '0.0.0.0' else 'localhost'}:{port}/status"
        info(f"[Web] 正在打开浏览器访问: {url}")
        try:
            webbrowser.open(url)
        except Exception as e:
            error(f"[Web] 无法打开浏览器: {str(e)}")

    # 启动服务器
    info(f"[Web] 启动FastAPI服务器: {host}:{port}")

    # 使用支持重启的模式
    config = Config(app=app, host=host, port=port, log_config=log_config)
    server = Server(config)

    # 启动服务器线程
    server_thread = threading.Thread(target=server.run, daemon=True, name="uvicorn_server")
    server_thread.start()

    # 工作流固定点激活与 HTTP 监听解耦：/status 与机械臂调试不能等源码编译。
    threading.Thread(
        target=_mount_workflow_runtime,
        daemon=True,
        name="workflow_runtime_mount",
    ).start()

    # info("[Web] Server started, monitoring for restart requests...")

    # 监控重启标志
    import unilabos.app.main as main_module

    while server_thread.is_alive():
        if hasattr(main_module, "_restart_requested") and main_module._restart_requested:
            info(
                f"[Web] Restart requested via WebSocket, reason: {getattr(main_module, '_restart_reason', 'unknown')}"
            )
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
