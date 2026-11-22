#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""serve_dashboard.py —— 极简静态服务，把 dashboard.html + votes.csv 提供出去

单独运行： python serve_dashboard.py [端口]
然后浏览器打开 http://127.0.0.1:8770/
"""
import http.server
import os
import socketserver
import sys
from pathlib import Path


def bundle_dir():
    """打包成 exe 后资源在 _MEIPASS，正常运行在脚本目录"""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def data_dir():
    return Path(os.environ.get("VOTE_TRACKER_HOME")
                or getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


class Handler(http.server.SimpleHTTPRequestHandler):
    """html 从资源目录取，csv 从数据目录取"""

    def translate_path(self, path):
        rel = path.split("?", 1)[0].split("#", 1)[0].lstrip("/")
        if rel in ("", "index.html"):
            rel = "dashboard.html"
        for base in (data_dir(), bundle_dir()):
            p = base / rel
            if p.is_file():
                return str(p)
        return str(bundle_dir() / rel)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass  # 静音，避免刷屏


def make_server(port=8770):
    socketserver.TCPServer.allow_reuse_address = True
    return socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8770
    with make_server(port) as httpd:
        print("看板地址: http://127.0.0.1:%d/" % port, flush=True)
        httpd.serve_forever()


if __name__ == "__main__":
    main()
