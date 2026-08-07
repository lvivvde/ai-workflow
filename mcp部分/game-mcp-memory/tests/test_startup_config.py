import socket

from app.config import Settings


def test_default_listener_address_can_be_bound():
    settings = Settings(_env_file=None)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((settings.memory_host, settings.memory_port))
