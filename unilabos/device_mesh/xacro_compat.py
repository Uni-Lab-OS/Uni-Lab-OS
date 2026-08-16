"""旧式 Xacro 与已展开包模型之间的窄兼容边界。"""

from __future__ import annotations

from lxml import etree

try:
    import xacro
except ModuleNotFoundError:  # pragma: no cover - exercised through the helper
    xacro = None


def render_robot_xml(root: etree._Element, *, description: str) -> str:
    """展开旧式 Xacro，或直接序列化已经完整展开的包模型。"""

    source = etree.tostring(root, encoding="unicode")
    requires_xacro = any(
        isinstance(element.tag, str)
        and element.tag.startswith("{http://ros.org/wiki/xacro}")
        for element in root.iter()
    )
    if not requires_xacro:
        return source
    if xacro is None:
        raise RuntimeError(
            f"{description} 包含旧式 Xacro 元素，但当前 Python 环境未安装 xacro"
        )
    document = xacro.parse(source)
    xacro.process_doc(document)
    return document.toxml()


__all__ = ["render_robot_xml"]
