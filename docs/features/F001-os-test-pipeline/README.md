# F001 — OS 自动化测试与发布门禁

本目录是 Q3 分层测试、模拟驱动和发布门禁的 feature dossier。当前状态为
**0/15，待认领**；这里的设计不能被引用为“已经上线”的能力。

## 阅读顺序

1. [`requirement.md`](requirement.md)：人类批准的目标、验收标准与范围。
2. [`interface-design.md`](interface-design.md)：接口和流水线设计。
3. [`feature-list.json`](feature-list.json)：可机器读取的任务拆分。
4. [`checklist.md`](checklist.md)：实现与验收检查。
5. [`progress.md`](progress.md)：当前真实进度、风险和待拍板事项。

## 核心边界

- Tier 1 必须在 Linux 运行真实 pytest，不以重试掩盖失败。
- driver test 使用 fake transport 和可控时钟，不连接硬件、不做墙钟 sleep。
- SZLab 可上收的是通用 OPC-UA 仿真引擎；设备契约数据留在设备包。
- release 必须受测试、semver、CHANGELOG 和兼容性声明约束。
- 本 feature 只负责 OS，不宣称覆盖前端或 backend 的完整测试体系。

任何进度更新都应同时修改 `progress.md` 和相应 checklist/feature item，禁止只凭一次本地
运行把未完成验收标为完成。
