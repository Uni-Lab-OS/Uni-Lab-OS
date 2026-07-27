# Device mesh assets

本目录是随 Uni-Lab-OS Python 包分发的设备与耗材模型资源根。local material server
通过 `MaterialModelRegistry` 登记需要暴露给前端的入口，并通过同源安全路由提供 XACRO、
URDF 及其 mesh 子资源。

## 目录约定

- `devices/<device>/`：设备 XACRO/URDF、mesh、joint/param 配置。
- `resources/<resource>/`：孔板、tip rack、容器等耗材模型。
- `ros2_controllers.yaml`、`view_robot.rviz`：ROS/RViz 配置，不是浏览器接口。
- `resource_visalization.py`：本地可视化辅助，不应进入 HTTP 请求链。

新增浏览器可用模型时：

1. 将完整、可再分发的模型和相对引用资源放入本目录；
2. 保持 XACRO/URDF 内 mesh 引用可由同一 asset root 解析；
3. 在 `unilabos/app/local_bridge/material_models.py` 增加显式 registry 项；
4. 启动时校验入口和 instance mesh；
5. 用真实 HTTP asset route 和前端 Pascal loader 验证完整依赖树。

## 坐标与实时状态

- 模型和 Material Graph 的静态 placement 分离。
- 模型转换用于统一坐标系/单位，不得承载某个实例的世界位置。
- 机械臂 joint pose 通过 realtime 通道更新；模型缺失实时值时使用 URDF 初始值。
- Site 可以位于运动 link 上，但 Site 静态定义不随 joint 更新。

## 绝对不能做

- 不得把宿主机绝对路径返回前端。
- 不得允许 `..`、符号链接或其他方式逃逸 asset root。
- 不得把某个验收模型的 camera、scene、scale 或位置写入 registry。
- 不得覆盖 Pascal 原生 scene、网格、灯光和通用 bounds framing。
- 不得把 Well/TipSpot 当作新模型的长期领域 Site。

相关 HTTP 契约与调用链见
[`../app/local_bridge/MATERIAL_API.md`](../app/local_bridge/MATERIAL_API.md)。
