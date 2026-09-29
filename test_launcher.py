import errno
import unittest
from unittest.mock import Mock, patch

import start_chatbot


class LauncherTest(unittest.TestCase):
    def test_existing_chatbot_is_reused(self):
        with patch.object(start_chatbot, "is_chatbot_running", return_value=True), \
                patch.object(start_chatbot, "build_pipeline") as build:
            self.assertEqual(start_chatbot.available_server(8765), (None, 8765))
        build.assert_not_called()

    def test_occupied_port_uses_next_port(self):
        server = Mock()
        with patch.object(start_chatbot, "is_chatbot_running", return_value=False), \
                patch.object(start_chatbot, "build_pipeline"), \
                patch.object(start_chatbot, "create_server", side_effect=[OSError(errno.EADDRINUSE, "busy"), server]) as create:
            self.assertEqual(start_chatbot.available_server(8765), (server, 8766))
        self.assertEqual(create.call_args.args[1], ("127.0.0.1", 8766))

    def test_unexpected_startup_errors_are_not_hidden(self):
        with patch.object(start_chatbot, "is_chatbot_running", return_value=False), \
                patch.object(start_chatbot, "build_pipeline"), \
                patch.object(start_chatbot, "create_server", side_effect=OSError(errno.EACCES, "denied")):
            with self.assertRaises(OSError):
                start_chatbot.available_server(8765)

    def test_non_chatbot_http_server_is_not_reused(self):
        response = Mock()
        response.headers = {"Server": "SomeOtherApp"}
        response.read.return_value = b"<title>Aster | unrelated page</title>"
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(start_chatbot, "urlopen", return_value=response):
            self.assertFalse(start_chatbot.is_chatbot_running(8765))

    def test_ports_exhausted(self):
        with patch.object(start_chatbot, "is_chatbot_running", return_value=False), \
                patch.object(start_chatbot, "build_pipeline"), \
                patch.object(start_chatbot, "create_server", side_effect=OSError(errno.EADDRINUSE, "busy")):
            with self.assertRaisesRegex(RuntimeError, "No free local port"):
                start_chatbot.available_server(65535)


if __name__ == "__main__":
    unittest.main()
