from unittest.mock import patch

from unilabos.app.communication import CommunicationClientFactory


def test_edge_control_protocol_uses_production_client_factory() -> None:
    sentinel = object()

    with patch.object(
        CommunicationClientFactory,
        "_create_edge_control_client",
        return_value=sentinel,
    ) as factory:
        client = CommunicationClientFactory.create_client("edge_control")

    assert client is sentinel
    factory.assert_called_once_with()


def test_supported_protocols_include_production_edge_control() -> None:
    assert CommunicationClientFactory.get_supported_protocols() == [
        "websocket",
        "edge_control",
    ]
