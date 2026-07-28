# OS Web 内部接口

`unilabos.app.web` 是 OS 进程的 FastAPI server，默认监听 `127.0.0.1:8002`。
`resource_templates.py` 提供给同机 `local_bridge` 的 Registry 模板接口：

```text
GET /internal/v1/resource-templates
GET /internal/v1/resource-templates/{template_uuid}
GET /internal/v1/resource-templates/{template_uuid}/assets/{asset_key}
```

这些路由不是浏览器 API。它们强制 loopback 来源；若设置
`UNILABOS_INTERNAL_API_TOKEN`，还必须携带同值 Bearer token。列表以 catalog revision
作为 ETag，详情以 content hash 作为 ETag，并支持 `If-None-Match`/304。

路由只做鉴权、条件请求和结构化错误映射；类型发现、公开规则、稳定 UUID、几何归一化与
安全资源解析都由 `registry/template_catalog.py` 负责。公共前端契约位于
`app/local_bridge` 的 `:8014/api/v1/resource-templates`。

不要在这里增加 Cloud panel 协议、当前 Material Graph 查询或模板写入；三者分别属于已删除
的旧协议、schedule snapshot 投影和未来统一写命令。
