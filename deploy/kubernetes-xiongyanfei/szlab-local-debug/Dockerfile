ARG BASE_IMAGE=tangshaodong/uni-lab-os-szlab:40bd6065-0098f5a-real-connection-status-20260815
FROM ${BASE_IMAGE}

ARG OS_REVISION=unknown
ARG SZLAB_REVISION=unknown
LABEL io.unilabos.commit=${OS_REVISION} \
      io.unilabos.szlab.commit=${SZLAB_REVISION} \
      io.unilabos.runtime-mode=local-debug

USER root

# 两个固定源码树使用全新目录并通过 PYTHONPATH 优先导入，避免在基础镜像旧目录
# 上做合并 COPY 后残留已从新提交删除的文件。
COPY unilabos /opt/runtime-src/os/unilabos
COPY --from=szlab package.yaml pyproject.toml /opt/runtime-src/szlab/
COPY --from=szlab szlab_poly_studio /opt/runtime-src/szlab/szlab_poly_studio
COPY --from=szlab deployment /opt/runtime-src/szlab/deployment

RUN chown -R 57439:57439 /opt/runtime-src

ENV PYTHONPATH=/opt/runtime-src/os:/opt/runtime-src/szlab

USER 57439:57439
WORKDIR /opt/runtime-src/szlab
