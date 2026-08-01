# Uni-Lab-OS Constructor 安装器

该目录使用 [conda constructor](https://conda.github.io/constructor/) 生成包含
Python、Conda、ROS 2 运行时和 Uni-Lab-OS 的离线安装器。默认安装生产包
`unilabos`；需要完整桌面、MoveIt、仿真和 Notebook 工具时可显式选择
`unilabos-full`。

## 平台映射

Constructor 的 `--platform` 必须使用 Conda subdir，而不能只写操作系统族名：

| 输入名 | 实际 Conda subdir | 原生构建系统 | 输出 |
| --- | --- | --- | --- |
| `linux`、`linux-64` | `linux-64` | Linux x86_64 | `.sh` |
| `osx`、`osx-64` | `osx-64` | macOS Intel | `.sh` |
| `osx-arm64` | `osx-arm64` | macOS Apple Silicon | `.sh` |
| `win-64` | `win-64` | Windows x86_64 | `.exe` |

`linux` 和 `osx` 是 GitHub Actions 手动输入接受的便捷别名，不是额外的
Conda subdir。Constructor 只能在目标原生系统上生成可发布安装器，因此 CI 使用
四种原生 runner。`--render` 可跨平台检查 selector；`--dry-run` 和正式构建都应在
目标原生系统执行，以便获得正确的 `__osx`、`__glibc` 等虚拟包。

## 本机构建

先安装固定版本的构建工具：

```bash
conda create -n constructor-build -c conda-forge constructor=3.16.1
conda activate constructor-build
```

从仓库根目录构建当前平台（版本默认与当前配置中的 `0.11.3` 一致）：

```bash
export UNILABOS_INSTALLER_VERSION=0.11.3
export UNILABOS_INSTALLER_PACKAGE=unilabos
constructor .conda/constructor \
  --platform linux-64 \
  --output-dir dist/constructor
```

只验证 selector、Schema 和依赖求解，不生成安装器：

```bash
constructor .conda/constructor --platform linux-64 --render
constructor .conda/constructor --platform linux-64 --dry-run
```

完整版构建时设置：

```bash
export UNILABOS_INSTALLER_PACKAGE=unilabos-full
```

GitHub Actions 的 `platforms` 输入接受逗号分隔的平台或别名，例如
`win-64,linux,osx-arm64,osx`。CI 会先进行原生依赖求解，再构建、静默安装并验证
`unilabos`、`rclpy` 和 `unilab --help`，最后上传安装器及 hash、lockfile 和包清单。
