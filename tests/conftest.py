import socket

import pytest


@pytest.fixture(autouse=True)
def forbid_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    original_socket = socket.socket

    class OfflineSocket(original_socket):
        def connect(self, address):
            raise AssertionError(f"offline test attempted a network connection: {address}")

        def connect_ex(self, address):
            raise AssertionError(f"offline test attempted a network connection: {address}")

    monkeypatch.setattr(socket, "socket", OfflineSocket)
