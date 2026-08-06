# SZLab Mock 设备包

这是一个可直接上传到 Uni-Lab 云端设备广场的测试设备包。它参考 SZLab S08 开关盖
工位的设备定义、初始化参数和 Action 形态，但全部行为只发生在进程内存中，不连接
PLC、OPC UA、串口或真实仪器。

## 固定身份

- distribution：`unilab-szlab-mock`
- version：`0.1.0`
- namespace：`community.unilab_szlab_mock`
- definition FQID：`community.unilab_szlab_mock.mock_s08_cap_station`
- 显示名称：`Mock S08 开关盖工位`

这些身份与真实 `community.szlab_poly_studio.*` 完全隔离，可用于判断问题来自设备包闭环
还是 SZLab 现场驱动/历史设备图。

## 本地检查

在 `Uni-Lab-OS` 仓库根目录执行：

```bash
unilab package inspect --path examples/szlab_mock_package
unilab package build --path examples/szlab_mock_package
```

本地启动可使用包内设备图：

```bash
unilab \
  --workspace examples/szlab_mock_package \
  --graph deployment/graphs/mock-szlab-local.json \
  --working_dir /tmp/unilab-szlab-mock-runtime \
  --config examples/szlab_mock_package/deployment/mock_config.py \
  --backend ros \
  --app_bridges fastapi \
  --visual disable \
  --disable_browser \
  --skip_env_check \
  --test_mode \
  --ros_domain_id 187
```

若命令提示不认识 `build`、`download` 或 `add-device`，先执行：

```bash
python -c 'import unilabos; print(unilabos.__file__)'
```

输出必须指向当前功能分支的 `Uni-Lab-OS/unilabos/__init__.py`；否则需要在当前仓库重新
执行 editable install，或先修正 Electron 配置的 CLI/Python 环境。旧环境会直接导致设备包
命令缺失，与 mock 包内容无关。

## Electron 完整闭环验证

1. 打开“设备广场 → 上传设备包”，选择本目录 `examples/szlab_mock_package`。
2. 选择测试、UAT 或正式环境，填写该环境对应的 AK/SK，检查后上传。
3. 在同一环境的云端设备广场搜索“Mock S08 开关盖工位”。
4. 添加到 Electron 本地心愿单并下载设备包。
5. 配置本地实例时可直接使用默认值；`channel_map` 可保留 `null` 或填写 JSON object。
6. 写入一个没有同名实例的设备图，启动本地 OS。
7. 在设备详情中确认 `ping`、`process_cap`、`read_status`、`reset` 四个 Action 可见。
8. 依次执行 `process_cap(operation="open", sample_id=1)` 与
   `process_cap(operation="close", sample_id=1)`；两次都应成功，`cycle_count` 依次为
   `1`、`2`，最终 `occupied_slots` 回到 `0`。

## 初始化参数建议

| 参数 | 建议值 | 含义 |
| --- | --- | --- |
| `station_name` | `Mock S08 Cap Station` | 本地显示名称 |
| `auto_connect` | `true` | 允许 Action 成功执行 |
| `cycle_delay_ms` | `20` | 模拟工艺耗时，范围 0–5000 ms |
| `cap_slot_count` | `5` | 模拟瓶盖暂存位总数 |
| `initial_occupied_slots` | `0` | 启动时已占用的暂存位数量 |
| `channel_map` | `null` 或 JSON object | 只验证 object 配置表单，不连接任何地址 |

若这个 mock 包能完成上传、心愿单、下载、写图和 Action 执行，而真实 SZLab 包失败，
应继续检查真实包的 FQID、历史设备图、依赖或驱动初始化；若 mock 包也失败，则优先检查
Electron/OS 的包缓存、设备图接管和 Registry 注册链路。
