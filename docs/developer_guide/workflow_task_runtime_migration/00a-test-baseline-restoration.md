# 00a 完整测试基线恢复门禁记录

状态：**完整测试门已通过，待三方独立评审，禁止合并**。

本轮是 Phase 01 的前置轮。用户要求每轮在独立分支执行，并且只有完整测试
通过、两名独立测试作者完成验证、三名独立评审者关闭阻塞项之后才允许合并。
Phase 00 已登记的完整测试收集债务因此不能继续延期到 Phase 02。

## 1. 轮次身份

| 字段 | 记录 |
|---|---|
| 轮次 | 00a |
| 实现范围 | 将重设计后遗留的旧测试迁到当前 Registry、community namespace、`init_param_enforce`、资源与液体处理公开合同，并恢复 `pytest tests/` 完整门禁 |
| 实现分支 | `migration/00a-test-baseline-restoration` |
| integration 基点 SHA | `37a86b98a67819a151cc0276fcb0792128b1cee5` |
| 主要证据 | `b005dc3` 删除 alias / `class.init` DSL；当前 `registry.py`、`initialize_device.py` 与 capability-source 当前测试 |
| 明确排除项 | Workflow Phase 01 production；恢复已删除的 alias 或 factory DSL；用 skip、xfail 或弱断言掩盖失败 |

## 2. 原始失败基线

标准命令：

```bash
/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q tests/
```

第一层结果：

```text
1 collection error
ModuleNotFoundError: No module named 'unilabos.registry.community_alias'
```

排除该遗留文件继续收集后，暴露第二个已删除模块
`unilabos.registry.external_registry_discovery`。将五个 Registry 重设计遗留测试
迁到当前公开入口后，完整收集恢复为：

```text
398 tests collected
```

完整执行进一步暴露的既有失败簇：

```text
18 failed, 349 passed, 3 skipped, 2 errors
```

失败只位于五个旧测试文件：

- `tests/devices/liquid_handling/test_transfer_liquid.py`
- `tests/integration/test_external_variant_construction.py`
- `tests/resources/test_bottle_carrier.py`
- `tests/resources/test_converter_bioyond.py`
- `tests/resources/test_resourcetreeset.py`

排除这五个已定位文件后，其余仓库结果为：

```text
373 passed, 3 skipped
```

## 3. 独立测试作者

| 角色 | subagent | worktree | 测试分支 | 来源测试 commit | 本分支 commit | 红/特征验证 |
|---|---|---|---|---|---|---|
| 合同测试 | `/root/phase00a_alias_contract_tests` | `/home/gaojing/.worktrees/uni-lab-os-00a-contract` | `test/00a-community-alias-contract` | `37e6439935e5cbef61cd9c1a786fd0e94dabe526`、`ca8f364487fa48d230ca4849d8122359e12d0d16` | `97c7faf`、`4881015` | 旧 alias import 红；旧 integration 为 `2 failed, 2 errors`；现行 direct namespace 与 JSON enforce integration 为 `5 passed` |
| 对抗测试 | `/root/phase00a_alias_adversarial_tests` | `/home/gaojing/.worktrees/uni-lab-os-00a-adversarial` | `test/00a-community-alias-adversarial` | `e0e5b6c6e0891712e2add5c00f0c994755ec53ce`、`1b8a005194dfb3e8b156637bf93ab82a906f568e`、`a19b20c93d98b16c3a79624f95a639a4388df65a` | `f263171`、`9bd6f0b`、`fcda715` | 旧 alias import 红；四个 device/resource 文件 `16 failed, 2 passed`；迁移后 `18 passed`，相关回归 `33 passed` |

两名测试作者均未查看或修改 production 实现；测试位于独立 worktree，
提交来源保留。本轮没有 production 文件改动。旧断言被迁到当前公开合同：

- `community.<ns>.<id>` 是实体 registry key，不做 stripped alias；
- registry 使用 JSON `init_param_enforce`，驱动负责从 JSON 构造富对象；
- liquid handling 测试替身实现当前 PLR `Container` 协议，原调用顺序、分批与体积断言保留；
- carrier 测试按当前直接资源 API 断言 identity 与 parent；
- BioYond fixtures 从测试文件目录读取，调试输出进入 `tmp_path`；
- `ResourceTreeSet` 增加单树与根名称的结果断言。

## 4. 当前门禁

| 门禁 | 命令 | 结果 |
|---|---|---|
| Registry targeted | `python -m pytest -q tests/registry` | `26 passed` |
| Registry + initialization integration | `python -m pytest -q tests/registry tests/integration/test_external_variant_construction.py` | `30 passed` |
| Device/resource targeted | liquid handling 与 `tests/resources` 相关回归 | `33 passed` |
| 完整收集 | `python -m pytest --collect-only -q tests/` | `398 tests collected` |
| 非失败簇回归 | 完整 `tests/` 排除上列五个已定位文件 | `373 passed, 3 skipped` |
| 完整仓库 tests | `python -m pytest -q -rs tests/` | `395 passed, 3 skipped` |
| Ruff E/F/I | `ruff check --select E,F,I --ignore E501`（本轮所有 Python 文件） | passed |
| `git diff --check` | `git diff --check` | passed |

三个 skip 均为 Phase 00 已登记的进程级联网慢测试，只能在设置
`UNILAB_NETWORKING_TEST=1` 的 CI 联网 job 中运行；本轮没有新增 skip、xfail
或豁免。

## 5. 独立评审与合并

候选 SHA：由本轮候选 commit 的 `workflow-migration` Git note 记录，确保测试与
评审可以绑定到不再变化的 commit 对象。

| 评审维度 | subagent | 评审 SHA | 状态 |
|---|---|---|---|
| 当前公开合同与能力源一致性 | 待分配 | | pending |
| 测试质量、仓库规范与覆盖保真 | 待分配 | | pending |
| 回归、隔离、环境与副作用 | 待分配 | | pending |

所有 blocking finding 必须修复、由原评审者复审，并针对同一个最终 SHA 重跑
完整门禁。未经明确授权不得 push。
