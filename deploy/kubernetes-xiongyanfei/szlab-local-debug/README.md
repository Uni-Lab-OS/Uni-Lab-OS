# SZLab 临时调试模式部署

本目录用于把当前 Uni-Lab-OS 与 Uni-Lab-SZLab 源码组合成一个本地镜像，并在
Kubernetes `xiongyanfei` 命名空间中以临时调试模式运行。

该模式显式使用 `--control_plane local`，只启用 `fastapi` bridge。它会启动内嵌
app/scheduler，不连接生产 Backend/Scheduler。`/runtime` 使用 `emptyDir`，Pod
重建后本地调试数据库和运行状态会丢失。

## 构建

在 Uni-Lab-OS 仓库根目录执行。构建使用两个固定提交的干净 detached
worktree，避免当前分支或未提交文件与镜像标签不一致：

```bash
set -euo pipefail

BUILD_ROOT="$(mktemp -d /home/xiongyanfei/.unilabos-szlab-build.XXXXXX)"
OS_SOURCE="$BUILD_ROOT/Uni-Lab-OS"
SZLAB_SOURCE="$BUILD_ROOT/Uni-Lab-SZLab"

cleanup_build_worktrees() {
  git worktree remove --force "$OS_SOURCE" >/dev/null 2>&1 || true
  git -C /home/xiongyanfei/Uni-Lab-SZLab \
    worktree remove --force "$SZLAB_SOURCE" >/dev/null 2>&1 || true
  rmdir "$BUILD_ROOT" >/dev/null 2>&1 || true
}
trap cleanup_build_worktrees EXIT

git worktree add --detach "$OS_SOURCE" \
  1f421617e92603d789a0fb62abd16812bcc29eae
git -C /home/xiongyanfei/Uni-Lab-SZLab worktree add --detach "$SZLAB_SOURCE" \
  8543f1a6ab683ec2c442783a48758f1cb89812b9

test "$(git -C "$OS_SOURCE" rev-parse HEAD)" = \
  1f421617e92603d789a0fb62abd16812bcc29eae
test -z "$(git -C "$OS_SOURCE" status --porcelain)"
test "$(git -C "$SZLAB_SOURCE" rev-parse HEAD)" = \
  8543f1a6ab683ec2c442783a48758f1cb89812b9
test -z "$(git -C "$SZLAB_SOURCE" status --porcelain)"

nerdctl -n k8s.io build \
  --build-context szlab="$SZLAB_SOURCE" \
  --build-arg OS_REVISION=1f421617e92603d789a0fb62abd16812bcc29eae \
  --build-arg SZLAB_REVISION=8543f1a6ab683ec2c442783a48758f1cb89812b9 \
  -f deploy/kubernetes-xiongyanfei/szlab-local-debug/Dockerfile \
  -t unilabos-szlab-local-debug:1f421617-8543f1a-r2 \
  "$OS_SOURCE"
```

镜像标签中的两段短 SHA 分别对应 Uni-Lab-OS `1f421617` 与 Uni-Lab-SZLab
`8543f1a`。

## 部署

以下清理命令会删除 `xiongyanfei` 命名空间内的所有资源和 PVC 数据。不得将
命名空间参数替换为其他值：

```bash
kubectl delete namespace xiongyanfei --wait=true
kubectl create namespace xiongyanfei
kubectl apply -f \
  /home/xiongyanfei/PLC-Sim/deploy/kubernetes-xiongyanfei/plc-sim.yaml
kubectl rollout status deployment/plc-sim -n xiongyanfei --timeout=5m
```

打开 `http://115.190.137.109:30160`，先在 GUI 中启动监听
`0.0.0.0:4855` 的 OPC UA Server，再用 `szlab` profile、`all` workflow 启动
SZLab Handshake Agent。GUI 状态确认两个进程均为 running 后再部署 Uni-Lab-OS：

```bash
kubectl apply -f deploy/kubernetes-xiongyanfei/szlab-local-debug/unilabos-local-debug.yaml
kubectl rollout status deployment/unilabos-local-debug -n xiongyanfei --timeout=10m
```

按当前部署要求，FastAPI 通过 NodePort 直接暴露到公网：

```text
http://115.190.137.109:30183
```

该调试接口没有 TLS 和访问认证，只应在明确接受该风险的临时调试环境使用。

源码中的 `szlab-local-debug.json` 仍保留本地调试 URL。Deployment 的一次性
initContainer 会等待 GUI 管理的 OPC UA Server 在 `plc-sim:4855` 就绪，然后只在
`emptyDir` 中生成 URL 为 `opc.tcp://plc-sim:4855` 的运行图，并将该临时目录覆盖到
工作区的 `deployment/graphs`；因此运行图仍位于 Uni-Lab-OS 允许的工作区边界内。
该过程不会修改源码，也不引入 TCP proxy sidecar。主进程恢复 PLC 自动连接，
PLC-Sim 未启动 Server 时会停留在初始化阶段而不会错误进入 Ready。
