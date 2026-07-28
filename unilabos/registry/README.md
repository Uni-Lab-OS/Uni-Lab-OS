# Registry 与前端模板目录

`Registry` 仍负责加载、校验和索引设备类型与资源类型。前端模板目录只是
`template_catalog.py` 对已经构建完成的 Registry 的只读公共投影。

## 数据来源与公开规则

- 设备来自 `devices/*.yaml` 和代码扫描后的 `device_type_registry`。
- 资源来自 `resources/` 加载后的 `resource_type_registry`。
- resource 默认 `public`；device 默认 `internal`。
- 设备要进入前端目录，必须在 Registry 条目/YAML 中显式写
  `catalog.visibility: public`。
- `hidden` 与 `internal` 都不出现在公共 list/detail 中。

可选 `catalog` 元数据包括：

```yaml
catalog:
  visibility: public
  source_namespace: unilabos
  display_name: PRCXI 液体工作站
  category: [移液, 自动化]
  tags: [liquid-handler]
  icon: device
  compatibility: {}
  ui_schema: {}
  assets:
    preview: relative/path.svg
```

资源路径必须相对 YAML 所在目录，且解析后仍位于该目录内。目录不公开 Python module
路径、动作 schema、host path、认证信息或完整 Registry entry。

## 稳定身份与详情

模板 UUID 是：

```text
UUID5(URL, "unilabos:resource-template:v1:{namespace}:{kind}:{key}")
```

列表返回轻量 summary 和基于内容的 `content_hash`；整个目录的 `revision` 由排序后的
公开身份与 content hash 生成。详情按需加载 resource 实现的 `config_info`，规范化为
毫米 geometry 和 grid/explicit `container_layout`。解析失败的详情标记
`status=unresolved`，不得伪造几何。

## HTTP 调用链

```text
Registry build
  -> ResourceTemplateCatalog
  -> :8002/internal/v1/resource-templates
  -> local_bridge ResourceTemplateProxy
  -> :8014/api/v1/resource-templates
  -> frontend Services
```

内部接口只接受 loopback；设置 `UNILABOS_INTERNAL_API_TOKEN` 后还要求 Bearer token。
浏览器不得直接访问内部接口。模板目录不依赖 schedule WS，也不读取当前设备图。

## 修改与验证

新增公开模板时优先补充声明元数据，不在投影器里按具体设备名写特例。至少运行：

```bash
UNILAB_PY=/home/changjunhan/.micromamba/envs/unilab/bin/python
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 "$UNILAB_PY" -m pytest \
  tests/registry/test_template_catalog.py \
  tests/app/test_resource_template_internal_api.py \
  tests/app/test_resource_template_proxy.py
```
