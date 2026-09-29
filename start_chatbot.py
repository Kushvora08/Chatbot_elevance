"""Start the local chatbot from its project folder, including on Windows double-click."""

import argparse
import errno
import os
from pathlib import Path
from threading import Timer
from urllib.error import URLError
from urllib.request import urlopen
import webbrowser

from kb_pipeline.server import DEFAULT_CONFIG_PATH, build_pipeline, create_server


def is_chatbot_running(port):
    try:
        with urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
            return ("KBPipelineConsole/" in response.headers.get("Server", "")
                    and b"<title>Aster |" in response.read(16384))
    except (OSError, URLError):
        return False


def available_server(port):
    pipeline = None
    for candidate in range(port, min(port + 10, 65536)):
        if is_chatbot_running(candidate):
            return None, candidate
        if pipeline is None:
            pipeline = build_pipeline(DEFAULT_CONFIG_PATH, None, None)
        try:
            return create_server(pipeline, ("127.0.0.1", candidate), config_path=DEFAULT_CONFIG_PATH), candidate
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE and getattr(exc, "winerror", None) != 10048:
                raise
    raise RuntimeError("No free local port found. Run again with --port 8780.")


def port_number(value):
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("Port must be between 1 and 65535.")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description="Start the Aster chatbot and open it in your browser.")
    parser.add_argument("--port", type=port_number, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    os.chdir(Path(__file__).resolve().parent)
    server, port = available_server(args.port)
    url = f"http://127.0.0.1:{port}"
    print(f"Chatbot: {url}", flush=True)
    print(f"Testing dashboard: {url}/console.html", flush=True)
    if server is None:
        print("The chatbot is already running.", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        return 0
    print("Keep this window open. Press Ctrl+C to stop the chatbot.", flush=True)
    # Delay the browser until the server enters its request loop.
    browser = None
    if not args.no_browser:
        browser = Timer(0.5, webbrowser.open, args=(url,))
        browser.daemon = True
        browser.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nChatbot stopped.")
    finally:
        if browser:
            browser.cancel()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
