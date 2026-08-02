# Round 02C：authority-scoped Template Catalog 公开接口冻结

日期：2026-08-01

实现分支：`migration/02c-template-catalog`

基线：`6845aee037e876f3ffd0eb2a146bbbec548ea381`

状态：**公开 seam 已冻结；在本提交之前不启动测试作者、不修改 production。**

## 1. 本轮目的

Round 02C 只实现 D-032、D-042 和总计划 02C 已经冻结的 Catalog 能力：

```text
显式 Registry/ResourceTemplate import 或 Backend sync
                         │
                         ▼
           authority-scoped replace transaction
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
WorkflowNodeTemplate             WorkflowHandleTemplate
真实、稳定 UUID                  真实、稳定 UUID
          └──────────────┬──────────────┘
                         ▼
         immutable compiler read snapshot
              + deterministic fingerprint
```

Catalog 是已发布模板身份与合同的 SQLite Authority；Registry parser、Backend
transport 和 Workflow Compiler 都是它的调用者，不是它的内部职责。

本轮不实现 HTTP、Catalog 网络同步、Action 合同到模板字段的完整投影、Workflow
Compiler、Draft/Apply、SSE、Frontend 或 Backend。

## 2. 冻结 public seam

新增 `unilabos.workflow.catalog`，只公开下列概念：

```python
CatalogAuthority(
    authority_id: str,
    kind: Literal["local", "backend"],
)

NodeTemplateImport(
    template: Mapping[str, object],
    handles: Sequence[Mapping[str, object]],
)

TemplateCatalog(store: WorkflowStore)

TemplateCatalog.replace(
    authority: CatalogAuthority,
    templates: Sequence[NodeTemplateImport],
) -> TemplateCatalogSnapshot

TemplateCatalog.snapshot(
    authority: CatalogAuthority,
) -> AbstractContextManager[TemplateCatalogSnapshot]
```

`TemplateCatalogSnapshot` 是不可变值，至少提供：

- `authority`；
- `fingerprint`，格式为 `sha256:<64-lowercase-hex>`；
- 按稳定顺序保存的 `node_templates` 和 `handle_templates`；
- `require_node(uuid)` 与 `require_handle(uuid, node_template_uuid=...)`；
- `assert_fingerprint(expected)`。

调用者只能通过快照读取 Catalog。`replace()` 是唯一写 seam；不存在“读不到就同步”
或 compiler callback。

## 3. Authority 与身份规则

`CatalogAuthority` 同时包含非空 `authority_id` 和显式 `kind`，两者共同进入
fingerprint。任何读写 SQL 都必须带 `authority_id` 条件；不得回退到另一个
authority。

### 3.1 Node business identity

一个 authority 内的 active Node 业务键为：

```text
(resource_template_uuid, lower(trim(name)))
```

### 3.2 Handle business identity

一个 authority 内的 active Handle 业务键为：

```text
(workflow_node_template_uuid, lower(trim(handle_key)), io_type)
```

`io_type` 只允许 `source` 或 `target`。大小写归一只用于匹配；快照保留被接受的
原始展示文本。

### 3.3 Local identity

- import 输入不得指定持久 Node/Handle UUID；嵌套 `handles` 已提供 parent 关联；
- 首次出现的 active 业务键由 OS 使用 UUIDv4 分配真实 UUID；
- 后续合同或显示字段更新按业务键 upsert，必须复用该 UUID；
- 进程重启后仍从 SQLite 复用，不允许 UUIDv5、进程内缓存或调用者 UUID；
- 已软删除的业务键日后重新出现时视为重新发布，分配新 UUID，不复活旧身份。

### 3.4 Backend identity

- 每个 Node 及 Handle 输入都必须携带合法 UUID；OS 精确保留 Backend UUID；
- 同一 active 业务键的合同更新复用 Backend 提供的同一 UUID；
- 同一 UUID 不得在一次或多次导入中改绑到另一个业务键或 parent；
- 一个 active 业务键换成新的 Backend UUID，表示上游重新发布：旧记录先软删除，
  新记录以精确上游 UUID 插入；
- 软删除后再次出现同一上游 UUID时可以恢复该 Backend 身份，因为身份 Authority
  仍是 Backend；OS 不生成或替换它。

## 4. Replace 事务与删除语义

一次 `replace(authority, templates)` 是该 authority 的**完整发布快照**，不是 patch：

1. 先在内存中完整校验和规范化输入；
2. 拒绝重复 Node/Handle 业务键、重复/改绑 UUID、跨 Node Handle、非法 `io_type`
   和缺少 Backend UUID；
3. 获取 Catalog guard；
4. 打开一个 `BEGIN IMMEDIATE` Store 事务；
5. upsert 本次所有 Node 与 Handle；
6. 将该 authority 中本次缺省的 active Handle 软删除；
7. 将缺省的 active Node 及其 Handle 软删除；
8. 从事务内最终 active 行计算 fingerprint，并原子写入 Catalog metadata；
9. commit 后返回同一内容的 immutable snapshot。

任一验证、SQL 或 fingerprint 步骤失败必须回滚整次 replace。另一个 authority 的
行、metadata 和 fingerprint 不得改变。

“模板生命周期由 ResourceTemplate aggregate 所有”在 02C 的含义是：上游 importer
提交完整 aggregate 投影；Catalog 只按完整投影做 replace。Catalog 不自行观察
Registry 删除，也不提供独立模板删除接口。

## 5. 持久结构

`workflow_node_template` 与 `workflow_handle_template` 保持现有 Backend-shaped 列，
增加 active partial unique indexes 以落实上述业务键。新增内部 metadata 表：

```text
workflow_template_catalog
  authority_id    PRIMARY KEY
  authority_kind  NOT NULL
  fingerprint     NOT NULL
  update_time     NOT NULL
```

metadata 行区分“尚未成功导入”与“成功导入了空 Catalog”。空 Catalog 是可用快照，
有确定 fingerprint；缺少 metadata 才是 unavailable。

时间戳、`deleted_at` 和 metadata 表自己的 `update_time` 不进入 fingerprint。

## 6. Fingerprint

fingerprint 输入为下列 versioned canonical payload 的 UTF-8 JSON：

```json
{
  "version": 1,
  "authority": {"authority_id": "...", "kind": "local|backend"},
  "node_templates": [],
  "handle_templates": []
}
```

- Node 按 UUID 排序；Handle 按 `(workflow_node_template_uuid, uuid)` 排序；
- 每行包含所有 active Backend-shaped identity/contract 字段；
- 排除 `create_time`、`update_time`、`deleted_at` 和外部包裹字段；
- JSON object key 排序并使用仓库统一 JSON codec；
- 对 canonical bytes 计算 SHA-256；
- 输入顺序、进程重启和无语义的重复 replace 不得改变 fingerprint；
- 任何 active identity/contract 字段、authority identity 或 kind 改变都必须改变它。

## 7. Read snapshot 与锁顺序

`snapshot(authority)` 是 context manager：

1. 获取该 `WorkflowStore` 唯一共享的 Catalog guard；
2. 在 Store 锁下读取 metadata 及全部 active 行；
3. 重新计算并核对 persisted fingerprint；
4. 产生不再访问 SQLite 的 immutable snapshot；
5. 在 context 退出前继续持有 Catalog guard。

因此 compiler 可在 context 内只读同一内存快照，`replace()` 会等待；Apply 可遵循
已经冻结的 `Catalog -> Store` 顺序，在 Catalog guard 内再打开 SQLite 写事务。
不得从 Store transaction callback 反向获取 Catalog guard。

同一 `WorkflowStore` 创建多个 `TemplateCatalog` facade 时也必须共享同一 guard，
不能各自创建互不知情的锁。一个 working directory 仍只允许一个 OS Workflow
Authority 进程；02C 不扩展为多进程锁协议。

context `__exit__` 只释放 guard，不做 I/O、同步或业务检查；02A 已有 service adapter
继续负责隔离异常的 cleanup guard。

## 8. 稳定错误

Catalog domain error 暴露稳定小写 code：

| 条件 | 异常 | code |
|---|---|---|
| authority 从未成功 replace | `TemplateCatalogUnavailable` | `template_catalog_unavailable` |
| metadata/active rows 自相矛盾或调用者请求不存在、跨 authority、错 parent 的身份 | `TemplateCatalogMismatch` | `template_catalog_mismatch` |
| `assert_fingerprint()` 与当前快照不同 | `TemplateCatalogStale` | `template_catalog_conflict` |
| import payload 非法或身份改绑 | `TemplateCatalogImportError` | `template_catalog_mismatch` |

异常还携带稳定、有限的 `path`，只定位 authority、Node 或 Handle identity；不得把
SQLite 消息、绝对路径、Backend body 或任意输入值拼进公共 diagnostic。

HTTP status、中文 message 和 compile diagnostic location 由 02D/02E adapter 映射，
不在 02C 内重复实现。

## 9. P0-4 停止线

02C 输入是**已经投影好的 Backend-shaped 模板 aggregate**，因此本轮：

- 可以持久和读取显式存在的 ResourceSlot Handle；
- 不从 runtime example、旧 Registry `handles` heuristic 或普通参数名猜 Handle；
- 不重新解析 02B 的 AST；
- 不决定 D-100 canonical Action contract 到 `goal`、`result`、`schema`、`required`
  或 `meta_data` 的完整字段映射；
- 不创建 D-068/D-069 implicit same-name ResourceSlot output；
- 不创建 `ready` 或任何其他隐藏 Handle；
- 不在 read/compile 时修补缺失模板。

这些边界保证 02C 不假装关闭 P0-4 剩余产品设计。

## 10. 独立测试合同

唯一 test subagent 应只从本文件和冻结决策编写 public-seam tests，至少覆盖：

- 未导入与成功空 Catalog 的区别；
- authority 严格隔离和无 fallback；
- local Node/Handle UUID 首次分配、更新/重启保持、删除重建换 UUID；
- Backend UUID 精确保留、缺 UUID/改绑/跨 parent 拒绝；
- 完整 replace 的 omission soft-delete 与事务回滚；
- 输入乱序、重复 replace、重启后的 deterministic fingerprint；
- identity/contract 字段变化触发 fingerprint 变化，时间戳不触发；
- snapshot immutable，且持有期间并发 replace 被 guard 阻塞；
- 多 facade 共用同一 Store guard；
- unavailable/mismatch/stale 的稳定 code/path；
- reader 与 compiler seam 不触发 importer、网络或 Registry side effect；
- P0-4 停止线：不合成 implicit/ready/heuristic Handle。

测试先提交 RED；主执行只实现使冻结合同通过的最小 production，不修改测试来适配
实现。

## 11. 本轮门禁

完成实现后依次执行：

```text
目标 02C tests
tests/workflow
tests/registry + tests/workflow
完整 tests/
本轮修改 Python 文件完整 Ruff
Ruff format --check
git diff --check
```

然后由本轮唯一独立 reviewer 同时检查 Standards 与 Spec。所有 blocking 关闭、门禁
保持全绿后才允许合并并生成中文趋势报告；合并后直接进入 02D，不再新增 02C 数字
子轮次。
