"""打开本机 SSH 通道并在浏览器中访问 3x-ui。"""

import argparse
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import webbrowser


def ssh_args(access, interactive=False):
    args = ["ssh", "-F", "none", "-o", "StrictHostKeyChecking=yes",
            "-o", "ForwardAgent=no", "-o", "ConnectTimeout=12",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            "-o", f"BatchMode={'no' if interactive else 'yes'}",
            "-p", str(access["ssh_port"]), "-l", access["ssh_user"]]
    if access.get("identity"):
        args += ["-i", access["identity"], "-o", "IdentitiesOnly=yes"]
    if access.get("known_hosts"):
        name = Path(access["known_hosts"]).as_posix()
        args += ["-o", f'UserKnownHostsFile="{name}"']
    return args


def wait_for_panel(process, port, path):
    until = time.monotonic() + 22
    while process.poll() is None and time.monotonic() < until:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        try:
            connection.request("GET", path)
            with connection.getresponse() as response:
                if response.status == 200 and process.poll() is None:
                    return True
        except (OSError, http.client.HTTPException):
            pass
        finally:
            connection.close()
        time.sleep(0.25)
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true", help="只打开并检查 SSH 通道")
    opts = parser.parse_args()
    access = json.loads(Path(__file__).with_name("connection.json").read_text(encoding="utf-8"))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        local_port = probe.getsockname()[1]
    command = ssh_args(access) + ["-N", "-T", "-o", "ExitOnForwardFailure=yes", "-L",
                                  f"127.0.0.1:{local_port}:127.0.0.1:{access['panel_port']}",
                                  access["ssh_host"]]
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        if not wait_for_panel(process, local_port, access["panel_path"]):
            print("面板未响应，请检查 SSH 密钥、指纹记录和服务器 x-ui 服务。")
            return 1
        url = f"http://127.0.0.1:{local_port}{access['panel_path']}"
        if not opts.no_browser and not webbrowser.open(url):
            # 只向运行启动器的人显示地址。
            print(url)
        print("面板通道已连接。账号见 panel-login.txt，按 Ctrl+C 关闭通道。", flush=True)
        return process.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit("面板启动失败，请检查本机连接资料、Python 和 OpenSSH。") from None
