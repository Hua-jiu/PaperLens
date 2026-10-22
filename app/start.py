"""Run directly or import main(). Set defaults here; no command-line arguments."""

from __future__ import annotations

import os
import errno
import threading
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

DEFAULT_PARAMS = {
    "host": "127.0.0.1",
    "port": int(os.environ.get("PAPER_READ_PORT", "8765")),
    "data_dir": (Path(__file__).resolve().parent.parent if Path(__file__).resolve().parent.name == "app" else Path(__file__).resolve().parent) / "data",
    "open_browser": os.environ.get("PAPER_READ_OPEN_BROWSER", "1") == "1",
    "dryrun": os.environ.get("PAPER_READ_DRYRUN", "0") == "1",
}


def main(host=DEFAULT_PARAMS["host"], port=DEFAULT_PARAMS["port"],
         data_dir=DEFAULT_PARAMS["data_dir"], open_browser=DEFAULT_PARAMS["open_browser"],
         dryrun=DEFAULT_PARAMS["dryrun"]):
    if __package__:
        from .backend.server import create_server
    else:
        from backend.server import create_server

    url = f"http://{host}:{port}"
    try:
        server = create_server(host, port, Path(data_dir), dryrun=dryrun)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE and getattr(exc, "winerror", None) != 10048:
            raise RuntimeError(f"无法启动本机服务：{exc.strerror or '没有监听端口的权限'}") from None
        try:
            import json
            with urlopen(url + "/api/health", timeout=2) as response:
                existing = json.load(response)
            if existing.get("app") != "paper-read":
                raise RuntimeError("该端口已被其他程序占用，请修改 app/start.py 顶部的 port")
            if bool(existing.get("dry_run")) != bool(dryrun):
                raise RuntimeError("该端口已有不同运行模式的阅读器，请先停止已有服务或修改顶部 port")
            print(f"论文阅读器已在运行：{url}")
            if open_browser:
                webbrowser.open(url)
            return
        except (OSError, URLError):
            raise RuntimeError(f"端口 {port} 已被占用，请修改 app/start.py 顶部的 port") from None
    print(f"论文阅读器：{url}")
    print("运行模式：dryrun（全部数据仅存内存，AI 网络请求停用）" if dryrun else f"数据目录：{Path(data_dir)}")
    print("按 Ctrl+C 停止服务。")
    if open_browser:
        threading.Timer(.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=.3)
    except KeyboardInterrupt:
        print("\n正在关闭论文阅读器…")
    finally:
        server.server_close()
        server.application.close()


if __name__ == "__main__":
    main()
