# 00a 完整测试基线恢复门禁记录

状态：**首次三方评审阻塞项已修复，待原评审者复审，禁止合并**。

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
| 合同测试 | `/root/phase00a_alias_contract_tests` | `/home/gaojing/.worktrees/uni-lab-os-00a-contract` | `test/00a-community-alias-contract` | `37e6439`、`ca8f364`、`e48b85a`、`55bc01c`、`d2ce95a` | `97c7faf`、`4881015`、`60ed04c`、`52f9caa`、`467ea98` | alias/import 与旧 integration 红；旧 external fixture 上新合同 `4 failed, 4 passed`；JSON enforce fixture 与完整加载/构造链迁移后绿 |
| 对抗测试 | `/root/phase00a_alias_adversarial_tests` | `/home/gaojing/.worktrees/uni-lab-os-00a-adversarial` | `test/00a-community-alias-adversarial` | `e0e5b6c`、`1b8a005`、`a19b20c`、`55fc076`、`1100ce3` | `f263171`、`9bd6f0b`、`fcda715`、`d325a22`、`c0e1c7a` | device/resource 旧文件 `16 failed, 2 passed`；BioYond 强断言在旧反向 mapping 上 `4 failed`；现行 mapping、真实转换与隔离后绿 |

两名测试作者均未查看或修改 production 实现；测试位于独立 worktree，
提交来源保留。本轮没有 production 文件改动。旧断言被迁到当前公开合同：

- `community.<ns>.<id>` 是实体 registry key，不做 stripped alias；
- registry 使用 JSON `init_param_enforce`，驱动负责从 JSON 构造富对象；
- liquid handling 测试替身实现当前 PLR `Container` 协议，原调用顺序、分批与体积断言保留；
- carrier 测试按当前直接资源 API 断言 identity 与 parent；
- BioYond fixtures 从测试文件目录读取，调试输出进入 `tmp_path`；
- `ResourceTreeSet` 断言 material/detail 节点、父 UUID 和容器层级；
- external variant fixture 使用 `init_param_schema + init_param_enforce`，并经过
  discovery → Registry load → `initialize_device_from_dict` → driver 的真实完整链；
- 需要完整 Registry 的测试在独立子进程执行，线程池显式关闭，不再在收集期
  调用全局 `lab_registry.setup()`。

## 4. 当前门禁

| 门禁 | 命令 | 结果 |
|---|---|---|
| Registry targeted | `python -m pytest -q tests/registry` | 含完整链的累积门禁通过 |
| Registry + initialization integration | `python -m pytest -q tests/registry tests/integration/test_external_variant_construction.py` | `31 passed` |
| Device/resource targeted | liquid handling 与 `tests/resources` 相关回归 | `33 passed` |
| Registry + integration + resources | `python -m pytest -q tests/registry tests/integration/test_external_variant_construction.py tests/resources` | `51 passed` |
| 完整收集 | `python -m pytest --collect-only -q tests/` | `399 tests collected` |
| 非失败簇回归 | 完整 `tests/` 排除上列五个已定位文件 | `373 passed, 3 skipped` |
| 完整仓库 tests | `python -m pytest -q -rs tests/` | `396 passed, 3 skipped` |
| Ruff E/F/I | `ruff check --select E,F,I --ignore E501`（本轮所有 Python 文件） | passed |
| `git diff --check` | `git diff --check` | passed |

三个 skip 均为 Phase 00 已登记的进程级联网慢测试，只能在设置
`UNILAB_NETWORKING_TEST=1` 的 CI 联网 job 中运行；本轮没有新增 skip、xfail
或豁免。

## 5. 独立评审与合并

首次候选 `e49a9529db68774bb784f981214b38a4e3d2069c` 虽然为
`395 passed, 3 skipped`，但三名独立评审者均判定 FAIL。其 Git note 保留失败
证据。修复后的候选 SHA 由本记录提交之后的 `workflow-migration` Git note
记录，确保测试与复审绑定到不再变化的 commit 对象。

| 评审维度 | subagent | 评审 SHA | 状态 |
|---|---|---|---|
| 当前公开合同与能力源一致性 | `/root/phase00a_contract_reviewer` | `e49a952` | FAIL；待复审 |
| 测试质量、仓库规范与覆盖保真 | `/root/phase00a_test_quality_reviewer` | `e49a952` | FAIL；待复审 |
| 回归、隔离、环境与副作用 | `/root/phase00a_regression_reviewer` | `e49a952` | FAIL；待复审 |

首次评审阻塞项与处置：

| Finding | 处置 | 修复 commit |
|---|---|---|
| external fixtures 与旧测试仍保留 `class.init` factory DSL | 迁到 JSON enforce，断言 `class` 不含 `init` | `60ed04c`、`52f9caa`、`467ea98` |
| “full chain” 只加载不构造，绕过 YAML loader | 新增真实 fixture 的 discovery/load/initialize/driver 双变体链 | `60ed04c` |
| BioYond 转换输出为空仍会通过 | 红测锁定非空、数量、模型、名称、metadata、site/parent/detail/tree | `d325a22`、`c0e1c7a` |
| Registry singleton、线程池和收集期 setup 泄漏 | 子进程隔离、executor 上下文关闭、移除 import-time setup | `c0e1c7a` |

所有 blocking finding 必须修复、由原评审者复审，并针对同一个最终 SHA 重跑
完整门禁。未经明确授权不得 push。
