from __future__ import annotations

from lxml import etree
import pytest

from unilabos.device_mesh import xacro_compat


def test_expanded_package_model_does_not_require_xacro(monkeypatch) -> None:
    monkeypatch.setattr(xacro_compat, "xacro", None)
    root = etree.fromstring(
        b'<robot xmlns:xacro="http://ros.org/wiki/xacro" name="robot">'
        b'<link name="world"/></robot>'
    )

    rendered = xacro_compat.render_robot_xml(
        root,
        description="机器人描述（URDF）",
    )

    assert '<link name="world"/>' in rendered


def test_legacy_xacro_fails_closed_when_dependency_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(xacro_compat, "xacro", None)
    root = etree.fromstring(
        b'<robot xmlns:xacro="http://ros.org/wiki/xacro" name="robot">'
        b'<xacro:include filename="legacy.xacro"/></robot>'
    )

    with pytest.raises(RuntimeError, match="未安装 xacro"):
        xacro_compat.render_robot_xml(
            root,
            description="机器人描述（URDF）",
        )
