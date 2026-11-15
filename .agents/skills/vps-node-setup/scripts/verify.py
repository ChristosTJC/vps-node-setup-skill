"""从当前机器用独立的 Mihomo 进程验证交付资料。"""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


TIMEOUT = 9
PROTOCOLS = {"vless", "vmess", "hysteria2"}
TARGETS = {
    "egress": ("api.ipify.org", "/?format=json", 200),
    "cloudflare": ("cp.cloudflare.com", "/generate_204", 204),
    "google": ("www.google.com", "/generate_204", 204),
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 订阅路径含有访问凭据，限制请求到原始 HTTPS 地址。
        return None


def error_kind(error):
    if isinstance(error, urllib.error.URLError):
        error = error.reason
    if isinstance(error, ssl.SSLCertVerificationError):
        return "tls_certificate"
    if isinstance(error, TimeoutError):
        return "timeout"
    return "connection"


def read_bundle(bundle):
    config = json.loads((bundle / "mihomo-config.json").read_text(encoding="utf-8"))
    status = json.loads((bundle / "status.json").read_text(encoding="utf-8"))
    subscription = (bundle / "subscription.txt").read_text(encoding="utf-8").strip()
    public_ip = ipaddress.ip_address(status["public_ip"])
    nodes = config["proxies"]
    if not isinstance(nodes, list) or len(nodes) != 3 or any(not isinstance(node, dict) for node in nodes):
        raise ValueError("需要三种协议各一个节点。")
    names = [node["name"] for node in nodes]
    if any(not isinstance(name, str) or not name.strip() for name in names):
        raise ValueError("节点名称无效。")
    if len(set(names)) != 3 or {node["type"] for node in nodes} != PROTOCOLS:
        raise ValueError("需要三种协议各一个节点，且节点名称互不相同。")
    if any(node.get("skip-cert-verify") or node.get("dialer-proxy") for node in nodes):
        raise ValueError("节点需使用证书校验和独立连接。")
    address = urllib.parse.urlsplit(subscription)
    if address.scheme != "https" or not address.hostname or address.username is not None or address.fragment:
        raise ValueError("订阅需要使用服务器生成的 HTTPS 地址。")
    return nodes, public_ip, subscription


def check_subscription(url):
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "ClashMeta/vps-node-setup"})
        with client.open(request, timeout=TIMEOUT) as response:
            data = response.read(262_145)
            # 这里只判断 HTTPS 是否返回代理文档。协议验证使用明确的三个导出节点。
            valid = len(data) <= 262_144 and bool(re.search(rb"(?m)^proxies:[ \t]*(?:\r?$|\[)", data))
            return {"ok": response.status == 200 and valid, "status": response.status, "looks_like_yaml": valid}
    except urllib.error.HTTPError as error:
        error.close()
        return {"ok": False, "status": error.code}
    except (OSError, http.client.HTTPException, ValueError) as error:
        return {"ok": False, "error": error_kind(error)}


def two_local_ports():
    with socket.socket() as proxy, socket.socket() as control:
        proxy.bind(("127.0.0.1", 0))
        control.bind(("127.0.0.1", 0))
        return proxy.getsockname()[1], control.getsockname()[1]


def controller(port, token, path, selection=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        headers = {"Authorization": "Bearer " + token}
        if selection is None:
            connection.request("GET", path, headers=headers)
        else:
            headers["Content-Type"] = "application/json"
            connection.request("PUT", path, json.dumps({"name": selection}), headers)
        with connection.getresponse() as response:
            if selection is not None and response.status == 204:
                return None
            if selection is None and response.status == 200:
                return json.loads(response.read(8_193))
            raise OSError("Mihomo 控制接口返回异常。")
    finally:
        connection.close()


def wait_for_mihomo(process, port, token):
    deadline = time.monotonic() + 15
    while process.poll() is None and time.monotonic() < deadline:
        try:
            return controller(port, token, "/version")["version"]
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError):
            time.sleep(0.2)
    raise OSError("Mihomo 控制接口启动失败。")


def check_target(name, proxy_port, proxy_auth, public_ip):
    host, path, expected_status = TARGETS[name]
    connection = http.client.HTTPSConnection("127.0.0.1", proxy_port, timeout=TIMEOUT)
    # CONNECT 明确使用这个 Mihomo 实例，目标站证书由 HTTPSConnection 验证。
    connection.set_tunnel(host, port=443, headers={"Proxy-Authorization": proxy_auth})
    started = time.monotonic()
    result = {"ok": False}
    try:
        connection.request("GET", path, headers={"Connection": "close", "User-Agent": "vps-node-setup"})
        with connection.getresponse() as response:
            result.update(status=response.status, ok=response.status == expected_status)
            if name == "egress" and result["ok"]:
                result["ok"] = False
                actual = ipaddress.ip_address(json.loads(response.read(1_025))["ip"])
                result["ok"] = actual == public_ip
                if result["ok"]:
                    result["ip"] = str(actual)
                else:
                    result["error"] = "egress_ip_mismatch"
    except (ValueError, KeyError, TypeError):
        result["error"] = "invalid_response"
    except (OSError, http.client.HTTPException) as error:
        result["error"] = error_kind(error)
    finally:
        connection.close()
    result["seconds"] = round(time.monotonic() - started, 3)
    return result


def verify_nodes(bundle, executable, nodes, public_ip, interface, report):
    mixed, control = two_local_ports()
    token = secrets.token_urlsafe(24)
    password = secrets.token_urlsafe(24)
    proxy_auth = "Basic " + base64.b64encode(("verify:" + password).encode("ascii")).decode("ascii")
    group = "VPS-VERIFY-" + secrets.token_hex(4)
    config = {
        "mixed-port": mixed,
        "external-controller": f"127.0.0.1:{control}",
        "secret": token,
        "authentication": ["verify:" + password],
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        "mode": "rule",
        "log-level": "silent",
        "ipv6": False,
        "tun": {"enable": False},
        "profile": {"store-selected": False, "store-fake-ip": False},
        "proxies": nodes,
        "proxy-groups": [{"name": group, "type": "select", "proxies": [node["name"] for node in nodes]}],
        "rules": ["MATCH," + group],
    }
    if interface:
        config["interface-name"] = interface

    with tempfile.TemporaryDirectory(prefix=".mihomo-verify-", dir=bundle) as directory:
        config_path = Path(directory) / "verify.yaml"
        config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        process = subprocess.Popen(
            [str(executable), "-d", directory, "-f", str(config_path)],
            cwd=directory, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            report["mihomo_version"] = wait_for_mihomo(process, control, token)
            with ThreadPoolExecutor(max_workers=len(TARGETS)) as pool:
                for node in nodes:
                    controller(control, token, "/proxies/" + group, selection=node["name"])
                    checks = {name: pool.submit(check_target, name, mixed, proxy_auth, public_ip) for name in TARGETS}
                    results = {name: task.result() for name, task in checks.items()}
                    report["nodes"][node["type"]] = results
                    states = "，".join(f"{name} {'通过' if result['ok'] else '未通过'}" for name, result in results.items())
                    print(node["type"] + "  " + states, flush=True)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def save_report(bundle, report):
    handle, path = tempfile.mkstemp(prefix=".verify-report-", dir=bundle)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(path, bundle / "verification.json")
    finally:
        Path(path).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True, help="保存订阅和状态的本机目录")
    parser.add_argument("--mihomo", type=Path, help="Mihomo 可执行文件；省略时查找 PATH")
    parser.add_argument("--interface", help="可选，指定 Mihomo 连接服务器时使用的系统网卡名称")
    args = parser.parse_args()
    bundle = args.bundle.resolve()
    try:
        nodes, public_ip, url = read_bundle(bundle)
    except (OSError, ValueError, KeyError, TypeError):
        print("交付资料无效。检查配置、服务器 IP、三个节点与 HTTPS 订阅文件。")
        return 2

    report = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "public_ip": str(public_ip),
        "network": {"scope": "运行验证脚本的机器和网络", "mihomo_interface": args.interface,
                    "test_instance_tun": False, "subscription_proxy": "系统路由，无显式代理"},
        "subscription": check_subscription(url),
        "nodes": {},
    }
    print("HTTPS 订阅  " + ("通过" if report["subscription"]["ok"] else "未通过"), flush=True)
    command = args.mihomo or shutil.which("mihomo") or shutil.which("verge-mihomo")
    executable = Path(command).resolve() if command else None
    if not executable or not executable.is_file() or (os.name == "nt" and executable.suffix.lower() != ".exe"):
        report["local_error"] = "mihomo_not_found"
        print("请通过 --mihomo 指定兼容的 Mihomo 可执行文件。")
    else:
        try:
            verify_nodes(bundle, executable, nodes, public_ip, args.interface, report)
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            report["local_error"] = "mihomo_execution_failed"
            print("独立 Mihomo 进程或控制接口运行失败。请检查内核版本及节点配置。")

    complete = set(report["nodes"]) == PROTOCOLS
    report["node_egress_ok"] = complete and all(row["egress"]["ok"] for row in report["nodes"].values())
    report["all_target_checks_ok"] = complete and all(check["ok"] for row in report["nodes"].values() for check in row.values())
    try:
        save_report(bundle, report)
    except OSError:
        print("无法写入验证记录。请检查交付目录的写入权限。")
        return 2
    print("验证记录已保存为 verification.json。")
    if "local_error" in report:
        return 2
    return 0 if report["node_egress_ok"] and report["subscription"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
