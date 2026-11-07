"""Opt-in fixture for modules using synthetic evaluators or injected provider dispatch."""
import socket

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny_connect(*args, **kwargs):
        raise AssertionError("network connections are forbidden in synthetic tests")

    monkeypatch.setattr(socket.socket, "connect", deny_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_connect)
    monkeypatch.setenv("CQA_OFFLINE", "1")
    monkeypatch.setenv("CQA_FALLBACK_MODEL", "none")
