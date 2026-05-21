# Trace: Uni-Lab-OS

### EARS — Progress (2026-05-18 14:30)
<!-- concepts: editable-install, conda-env-shadowing -->
踩坑：conda `unilab` env 里有 pip 装的 unilabos 旧版本（site-packages），GitHub repo 的修改不会被加载。必须在 repo 根目录 `pip install -e .` 覆盖。否则跑的还是云端 bohrium 地址，本地异常处理代码完全不生效。

### EARS — Fix (2026-05-18 16:12)
<!-- concepts: silent-except, factory-naming -->
踩坑：`_handle_device_exception` 中 `_get_ws_client()` 始终返回 None，导致设备异常报警从未上行、前端一直 loading。根因是 `_get_ws_client()` 写的是 `from unilabos.app.communication import CommunicationFactory`，但实际类名是 `CommunicationClientFactory`。`ImportError` 被裸 `except Exception: return None` 静默吞掉。教训：禁止 `except Exception` 兜底返回 None —— 让真实错误自然抛出才能定位问题，符合用户 CLAUDE.md 中"严格控制 try/except"原则。
