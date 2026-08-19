import threading

from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.utils import logger


# 根据选择的 backend 启动相应的功能
def start_backend(
    backend: str,
    devices_config: ResourceTreeSet,
    resources_config: ResourceTreeSet,
    resources_edge_config: list[dict] = [],
    graph=None,
    controllers_config: dict = {},
    bridges=[],
    is_slave: bool = False,
    visual: str = "None",
    resources_mesh_config: dict = {},
    **kwargs,
):
    if backend == "ros":
        from unilabos.ros.main_slave_run import main, slave  # 如果选择 'ros' 作为 backend
    elif backend in ("simple", "automancer"):
        # 这两个 backend 尚未实现：原先的分支只是 pass，随后 target=main 会读到未绑定的
        # 局部变量，抛出 UnboundLocalError。那个报错既不说明选了没实现的 backend，也不
        # 提示该改用哪个，排查起来要一路读到这里才明白。改为在此处就说清楚。
        raise NotImplementedError(
            f"backend '{backend}' 尚未实现，当前只有 'ros' 可用；请使用 --backend ros。"
        )
    else:
        raise ValueError(f"Unsupported backend: {backend}")

    backend_thread = threading.Thread(
        target=main if not is_slave else slave,
        args=(
            devices_config,
            resources_config,
            resources_edge_config,
            graph,
            controllers_config,
            bridges,
            visual,
            resources_mesh_config,
        ),
        name="backend_thread",
        daemon=True,
    )
    backend_thread.start()
    logger.info(f"Backend {backend} started.")
