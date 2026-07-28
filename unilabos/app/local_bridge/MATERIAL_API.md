# Local Material API 与 backend 对照

本文记录 2026-07-28 当前代码的真实能力。对照源为：

- OS：`unilabos/app/main.py`、`app/ws_client.py`、
  `resources/material_state.py`、`app/local_bridge/local_api.py`、
  `material_api.py`、`material_models.py`、`resource_template_api.py`、
  `app/web/resource_templates.py`、`registry/template_catalog.py`、
  `schedule_ws.py`、`server.py`；
- Go backend：`internal/http/handler/resource.go`、
  `internal/service/resource/`、`internal/repository/resource.go`、
  `internal/model/resource.go`、`internal/model/state.go`。

Go backend 仓库仅用于接口核对，本文档工作不修改其代码。

## 结论

OS 与 backend 可服从同一前端 typed port 和 capability matrix，但当前**不是同一套物料
写服务**：

- OS 用 `unilab -g/--graph` 加载并在同一个 `ResourceTreeSet` 中维护当前状态；
  local API 从该内存权威生成只读 `MaterialAggregate` 投影；
- backend 是 PostgreSQL/GORM 上的模板、material、relative position、site、物质与状态行
  CRUD；
- 路径同为 `/api/v1/materials` 不等于返回语义相同；
- 前端当前可对 OS 声明 `material.readGraph` 与
  `material.readTemplates`；Go backend 的现有模板接口尚未满足本轮统一目录
  contract，因此当前 profile 不声明模板能力；
- revision、幂等键、原子聚合命令、失败补偿和 edge/backend 同步没有统一之前，两端都
  不得声明前端 Material Graph 写能力。

## OS local server 接口

默认 unified server 为 `http://127.0.0.1:8014`。真实模式要等 OS schedule session
连入；offline 模式会立即建立进程内 session。模型登记在 bridge 构造时完成，但 API 仍以
已就绪 `LocalApiState` 为边界。

| 方法与路径 | 参数 | 返回数据 | 能力 |
|---|---|---|---|
| `GET /health` | 无 | `{"status":"ok"}` | 进程健康，不代表物料图已配置 |
| `GET /api/v1/materials` | `page`、`page_size`、`name`、`code`、`resource_template_uuid` | `items/total/page/page_size` | 分页读取聚合投影 |
| `GET /api/v1/materials/{uuid}` | 稳定 UUID | 单个聚合投影行 | 按 ID 读取 |
| `GET /api/v1/material-models` | 无 | 已登记模型清单 | 模型能力发现 |
| `GET /api/v1/material-models/assets/{path}` | 登记根内相对路径 | XACRO/URDF/mesh 等文件 | 同源安全资源读取 |
| `GET /api/v1/resource-templates` | 可选 `refresh=true` | 全量轻量模板目录、revision、stale | Edge Registry 模板发现 |
| `GET /api/v1/resource-templates/{uuid}` | 可选 `refresh=true` | 懒加载详情、geometry/layout/config/assets | 模板详情 |
| `GET /api/v1/resource-templates/{uuid}/assets/{key}` | 可选 `Range` | 显式登记的模板资源 | 同源安全资源读取 |

`page >= 1`，`1 <= page_size <= 100`。列表支持大小写无关的 name/code 包含过滤和模板 UUID
精确过滤。

成功 envelope：

```json
{
  "code": 0,
  "data": {},
  "message": "success"
}
```

material problem 通过 FastAPI `HTTPException.detail` 返回结构化对象，常见 code：

- `MATERIAL_GRAPH_UNAVAILABLE`：没有就绪 session、OS 未绑定当前物料树或快照刷新失败；
- `INVALID_MATERIAL_QUERY` / `INVALID_MATERIAL_UUID`：查询或 UUID 非法；
- `MATERIAL_NOT_FOUND`：稳定 UUID 不存在；
- `MATERIAL_MODELS_UNAVAILABLE`：模型 registry 未进入就绪 state；
- `INVALID_MATERIAL_MODEL_PATH`：资源路径逃逸模型根；
- `MATERIAL_MODEL_ASSET_NOT_FOUND`：资源不存在。

模板公共接口错误统一为根级
`{error:{code,message,retryable}}`，常见 code：

- `CATALOG_UNAVAILABLE`：Registry server 不可达且 bridge 没有缓存；
- `TEMPLATE_NOT_FOUND`：UUID 不存在或模板不是 public；
- `TEMPLATE_ASSET_NOT_FOUND`：资源 key 未声明、越界或文件不存在；
- `INVALID_CATALOG_RESPONSE`：上游未遵守目录 contract。

目录有缓存且发生可重试上游错误时仍返回 200，但 `data.stale=true`，并把 summary/detail
中的 `creation.available` 统一设为 false。目录和详情支持 ETag；bridge 用 5 秒 TTL
减少重复请求，过期后使用 `If-None-Match` 重验证。

### OS 投影语义

图文件只在 OS 启动时读取。真实运行中的权威是 `ResourceTreeSet`：

1. `unilab -g` 解析图并将同一个对象交给 backend/HostNode 和 `CurrentMaterialState`；
2. OS 内部资源操作可以修改该对象；
3. Material GET 通过 schedule 请求当前快照；
4. bridge 原子替换只读 `MaterialGraphCatalog` 缓存后完成投影。

因此手工修改启动 JSON 不会偷偷改变运行中的实验室；OS 内存变化会在下一次 GET 体现。
投影规则：

- ID 是以图路径、对象种类和源 node ID 生成的稳定 UUID5；
- graph revision 是当前序列化内存节点的非零 checksum；
- 顶层输出排除 deck-slot 包装 node 以及内部 Well/TipSpot node；
- placement 规范化为 `world`、`parent` 或 `site`；
- parent placement 的 anchor 固定为 root；
- 位于 deck slot 下的物料变成 site placement，并 follow-site；
- `config.rendering` 引用模型 registry 生成的同源 URL；
- 模型资产 URL 不暴露宿主机绝对路径。

当前实现仍会把 Well/TipSpot 子 node 放入所属物料的 `config.sites`，并保留源 node
`data`。这两项是迁移期兼容债务，不是未来领域契约：

- Well/TipSpot 不是长期领域 Site，新代码不得依赖其 Site 身份；
- 试剂、样品和容器内容应进入相应后端表；
- 其他低频状态使用 `material_state_history`；
- 不应继续扩展通用 `data` 袋。

OS 当前没有以下物料 HTTP 能力：模板 CRUD、material 写入、relative position CRUD、
site CRUD、物质记录、state history、统一 undo/redo 命令。

## 与 Go backend 的接口对照

下表中的 backend 路径均位于 `/api/v1`：

| 领域能力 | OS local server | Go backend | 兼容判断 |
|---|---|---|---|
| material 列表/详情 | `GET /materials[/{uuid}]`，聚合只读投影 | `GET /materials[/{uuid}]`，数据库 material 行 | 路径相同，语义不同 |
| material 创建/替换/删除 | 无 | `POST /materials`、`PUT/DELETE /materials/{uuid}` | 不兼容 |
| resource template | Registry 全量 summary、懒详情、ETag、stale、显式 asset | `/resource-templates` CRUD 与 handles | 路径相同，当前 contract 不同 |
| 静态相对位姿 | 聚合在 `config.placement` | `/materials/{uuid}/relative-position` CRUD | 需 adapter，非同一字段 |
| Site | 聚合在 `config.sites`，只读 | material 下 create/list/order；`/sites/{uuid}` get/update/delete | backend 为独立持久化实体 |
| 当前物质/历史 | 无 | `/materials/{uuid}/current-substance`、`substance-history` | backend only |
| 物料状态 | 无 | `/materials/{uuid}/states` append/list/latest；`/material-states/{uuid}` get | backend append-only history |
| 模型 registry/assets | `/material-models` 与安全 asset route | 模板 `model/scene/package_info` 等字段，无同名资源 route | 需明确模型分发契约 |
| 聚合 revision | 图 checksum | 当前行模型无统一 graph revision | 不能安全聚合写 |
| 原子命令/幂等/补偿 | 无 | 当前为多个行级请求 | 两端都不能声明统一 writeGraph |

backend material 行的主要字段是
`resource_template_uuid/code/name/config/data`。`RelativePosition` 独立保存位置、旋转、
尺寸和 scale；`Site` 独立保存所属 material、顺序、允许模板、占用 material、位置与尺寸。
因此 backend 的普通 material row 不保证可直接还原前端所需的
`config.placement/rendering/sites`。

backend 成功响应为 `{ "data": ... }`，错误为
`{ "error": { "code": "...", "message": "..." } }`；OS 的 envelope/problem 形状也
不同。解包差异可以由 adapter 处理，但不能借此改变业务能力。

## 坐标、Site 与实时状态

- 静态 placement 描述结构关系，不随机械臂高频关节更新。
- child 可以挂 root、普通 parent 或 Site，不要求最终都挂 Site。
- Site 表示设备的可安装位置。机械臂末端 Site 的静态定义不随 joint 高频变化；
  其世界 pose 在 3D runtime 中由当前关节链计算。
- joint 数据走单独 realtime 通道，只更新 Pascal/Three runtime object，不失效 ReactFlow
  或 Material Graph 查询。
- realtime joint 缺失时允许回退 URDF 初始值。
- 低频状态可追加到 backend `material_state_history`；需要高频完整记录时由 OS 内部
  rosbag/MCAP 等机制承担，而不是提高前端/数据库上报频率。

## OS 内部调用链

真实 OS 启动与首次快照：

```text
unilab -g graph.json
  -> app.main.parse_args()
  -> graphio.read_node_link_json/read_graphml()
  -> one ResourceTreeSet
  -> backend/HostNode mutate the same object
  -> communication client.bind_material_state(ResourceTreeSet)
  -> CurrentMaterialState
  -> OS connects schedule WS
  -> publish_host_ready()
  -> material_snapshot

local bridge
  -> MaterialModelRegistry()
  -> empty MaterialGraphCatalog()
  -> ScheduleSession.on_material_snapshot(catalog.replace_snapshot)
  -> LocalApiState
  -> LocalApiServer(:8014)
```

material 请求：

```text
FastAPI route in local_api.py
  -> LocalApiState.refresh_material_catalog()
  -> ScheduleSession.request_material_snapshot()
  -> query_material_snapshot over schedule WS
  -> WebSocketClient.publish_material_snapshot()
  -> CurrentMaterialState.snapshot(ResourceTreeSet)
  -> material_snapshot over schedule WS
  -> MaterialGraphCatalog.replace_snapshot()
  -> MaterialGraphCatalog.list_materials() / get_material()
  -> normalize nodes, placement, sites and rendering metadata
  -> success envelope
```

offline bridge 可使用 `--offline -g graph.json`。此时 bridge 作为进程内执行 OS，只在启动时
读取一次图文件，后续同样使用内存 catalog；真实模式若给 bridge 单独传 `--graph` 会
fail closed，避免出现第二个图权威。

model 请求：

```text
GET /api/v1/material-models
  -> MaterialModelRegistry.list_models()

GET /api/v1/material-models/assets/{path}
  -> MaterialModelRegistry.resolve_asset()
  -> reject path outside asset_root
  -> require regular file
  -> FileResponse
```

template 请求：

```text
GET :8014/api/v1/resource-templates[/{uuid}]
  -> local_api.py
  -> ResourceTemplateProxy
  -> TTL cache; expired entry adds If-None-Match
  -> GET :8002/internal/v1/resource-templates[/{uuid}]
  -> loopback/token authorization
  -> ResourceTemplateCatalog(lab_registry)
  -> already-built device_type_registry/resource_type_registry
  -> stable summary or lazy normalized detail
  -> ETag/revision back through bridge
  -> {code,data,message}

upstream temporarily unavailable
  -> cached entry exists: stale=true + disable creation
  -> no cached entry: structured 503 CATALOG_UNAVAILABLE
```

模板 asset 只能来自 YAML `catalog.assets` 显式键；internal route 将相对路径限制在该
YAML 所在目录，bridge 只转发允许的内容/Range header。设备默认 internal、resource
默认 public；当前仅 `liquid_handler.prcxi` 设备被显式公开。

该链路不经过 schedule WS，也不读取 `-g` 图。Registry 模板描述“可以创建什么”，
Material Graph 描述“OS 内存里当前有什么”，二者不能互相充当 fallback。

本调用链没有 repository/database 写层，这是有意的。OS 内部修改通过 ResourceTreeSet 的
设备/资源路径发生；HTTP API 保持只读。若未来需要前端写入，先定义 OS 与 backend 都能实现
的聚合命令、revision、幂等和补偿，再决定权威端；不能在 `material_api.py` 增加 JSON 写回。

## Backend 内部调用链

```text
cmd/server
  -> internal/app.New
  -> database.Open / Migrate
  -> repository.NewResource
  -> resource.New service
  -> http router
  -> handler.NewResource.RegisterRoutes

request
  -> Gin handler decode
  -> resource.Service validation and invariants
  -> repository.Resource
  -> GORM
  -> database
  -> response envelope
```

Site 放置由 service 校验允许模板、唯一占用、自引用和环路等不变量。
`MaterialStateHistory` 是 append-only 历史；高频 joint 不能写入 static relative position。

## 修改检查清单

- 是否仍由一个 Material Graph 驱动 2D、2.5D 和 3D？
- 是否仍只有 OS 的 ResourceTreeSet 可变，bridge 只缓存快照？
- 是否没有重新读取启动 graph 文件充当运行时权威？
- 是否只在完整实现 typed port 时打开 capability？
- 是否保留 singleton local scope，不伪造 `laboratoryId`？
- 是否没有把 Well/TipSpot 固化为领域 Site？
- 是否没有把 joint 流写回静态图或触发 ReactFlow 高频渲染？
- 是否所有 asset path 都限制在 registry root？
- 是否没有设备名、测试图或相机特例？
- 是否同时运行 OS material API test 与前端真实 material E2E？
- 模板是否来自已构建 Registry，而非当前图、Cloud 残留或前端静态数组？
- device 是否仍默认 internal，asset 是否仍限定在显式声明目录？
- ETag/revision 是否稳定，stale 是否禁用创建且无缓存时 fail closed？
