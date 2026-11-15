"""SSH 远端安装器。命令只管理由本 Skill 初始化的 VPS。"""

import argparse
from dataclasses import asdict, dataclass
import hashlib
import http.cookiejar
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import ssl
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile


WORK = Path("/var/lib/vps-node-setup")
STATE = WORK / "state.json"
PANEL = Path("/usr/local/x-ui")
CORE = PANEL / "bin/xray-linux-amd64"
UNIT = Path("/etc/systemd/system")
CERT_NAME = "vps-node-setup"
CERT = Path("/etc/letsencrypt/live") / CERT_NAME
CERTBOT = WORK / "certbot/bin/certbot"
SITE = Path("/etc/nginx/sites-available/vps-node-setup")
ENABLED = Path("/etc/nginx/sites-enabled/vps-node-setup")
PANEL_PORT, SUB_PORT, WS_PORT = 38471, 38472, 10000
SUPPORTED_OS = {("ubuntu", "24.04"): "Ubuntu 24.04", ("debian", "13"): "Debian 13"}
RELEASES = {
    "panel": (
        "https://github.com/MHSanaei/3x-ui/releases/download/v3.11.0/x-ui-linux-amd64.tar.gz",
        "0ddfa4702f595442e2962acd81addc768fdacaa4ff2672c971a0463dae744916",
    ),
    "xray": (
        "https://github.com/XTLS/Xray-core/releases/download/v26.11.9/Xray-linux-64.zip",
        "b651f7e1d9966ee2c1b1ee1bf8b52c72771824d3065dce96bbc8bcc19ed95f82",
    ),
}


class SetupError(Exception):
    pass


@dataclass(frozen=True)
class Spec:
    public_ip: str
    domain: str = ""
    reality_target: str = ""
    reality_port: int = 443
    https_port: int = 33417
    hy2_port: int = 46289
    enable_ufw: bool = False

    @property
    def endpoint(self):
        return self.domain or self.public_ip

    def sockets(self):
        return {
            ("tcp", 80), ("tcp", self.reality_port), ("tcp", self.https_port),
            ("udp", self.hy2_port), ("tcp", WS_PORT),
            ("tcp", PANEL_PORT), ("tcp", SUB_PORT),
        }

    def cloud_ports(self):
        return [f"{port}/{protocol}" for protocol, port in sorted(self.sockets())
                if (protocol, port) not in {("tcp", p) for p in [WS_PORT, PANEL_PORT, SUB_PORT]}]

    def check(self):
        ipaddress.IPv4Address(self.public_ip)
        for name in [self.domain, self.reality_target]:
            if name and not re.fullmatch(
                r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?",
                name,
            ):
                raise SetupError("域名必须是 DNS 主机名，例如 example.org；请省略协议、路径和端口。")
        for port in [self.reality_port, self.https_port, self.hy2_port]:
            if type(port) is not int or not 1 <= port <= 65535:
                raise SetupError("端口必须在 1–65535 之间。")
        if len(self.sockets()) != 7:
            raise SetupError("TCP 端口冲突：80、10000、38471、38472 留给证书和本机服务。")
        if self.reality_target and self.reality_target == self.domain:
            raise SetupError("REALITY 目标需指向可独立访问的站点。")


def note(message):
    print(message, file=sys.stderr, flush=True)


def run(*command):
    result = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=45)
    if result.returncode:
        raise SetupError(f"执行 {Path(command[0]).name} 失败；请检查该服务的本机日志。")
    return result.stdout


def logged(name, *command, timeout=300, env=None):
    with (WORK / f"{name}.log").open("wb") as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                timeout=timeout, env=env)
    if result.returncode:
        raise SetupError(f"{name} 失败，详见服务器 root 日志 {WORK / (name + '.log')}。")


def write(path, value, mode=0o600):
    pending = path.with_name(path.name + ".pending")
    pending.write_text(value, encoding="utf-8")
    pending.chmod(mode)
    pending.replace(path)


def save_state(state):
    write(STATE, json.dumps(state, indent=2) + "\n")


def read_state(spec):
    if not STATE.exists():
        return None
    state = json.loads(STATE.read_text(encoding="utf-8"))
    if state.get("spec") != asdict(spec):
        raise SetupError("这台 VPS 已有安装记录。请用原来的公网 IP、域名、目标和端口运行，配置调整可在面板中完成。")
    return state


def listeners():
    rows = []
    for line in run("ss", "-H", "-lntu").splitlines():
        fields = line.split()
        if len(fields) >= 5:
            address, _, port = fields[4].rpartition(":")
            if port.isdigit():
                rows.append((fields[0], int(port), address))
    return rows


def preflight(spec, ssh_server_port, state):
    os_release = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines()
                      if "=" in line)
    release = tuple(os_release.get(key, "").strip('"') for key in ("ID", "VERSION_ID"))
    system = SUPPORTED_OS.get(release)
    if (not system or os.uname().machine != "x86_64"
            or Path("/proc/1/comm").read_text().strip() != "systemd"):
        raise SetupError("支持 Ubuntu 24.04 LTS 和 Debian 13，需要 x86_64 与 systemd。")
    if ("tcp", ssh_server_port) in spec.sockets():
        raise SetupError("应用端口与 SSH 的服务器端监听重合，请调整应用端口。")
    if not state:
        occupied = {(proto, port) for proto, port, _ in listeners()} & spec.sockets()
        owned_paths = [PANEL, Path("/etc/x-ui"), UNIT / "x-ui.service",
                       UNIT / "vps-node-renew.service", UNIT / "vps-node-renew.timer",
                       SITE, ENABLED, Path("/etc/nginx/sites-enabled/default"),
                       CERT, Path("/etc/letsencrypt/renewal") / (CERT_NAME + ".conf")]
        existing = [str(path) for path in owned_paths if path.exists() or path.is_symlink()]
        if occupied or existing:
            busy = ", ".join(f"{port}/{proto}" for proto, port in sorted(occupied))
            raise SetupError("已有服务或文件占用安装位置，请检查：" + ", ".join(filter(None, [busy, *existing])))
    if spec.domain:
        answers = {row[4][0] for row in socket.getaddrinfo(spec.domain, 443, type=socket.SOCK_STREAM)}
        if answers != {spec.public_ip}:
            raise SetupError("域名需直接指向所填公网 IPv4（仅此 A 记录，关闭代理/CDN，移除未部署的 AAAA）。")
    return {"os": f"{system} x86_64", "ssh_server_port": ssh_server_port,
            "cloud_inbound": spec.cloud_ports(), "managed_install": bool(state),
            "deployment_complete": bool(state and state.get("complete"))}


def find_target(spec):
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.set_alpn_protocols(["h2", "http/1.1"])
    for host in [spec.reality_target] if spec.reality_target else ["www.kernel.org", "www.mozilla.org"]:
        try:
            peers = {row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
            if not peers or any(not ipaddress.ip_address(peer).is_global for peer in peers) or spec.public_ip in peers:
                continue
            for peer in sorted(peers, key=lambda value: (":" in value, value)):
                try:
                    with socket.create_connection((peer, 443), timeout=4) as sock:
                        with context.wrap_socket(sock, server_hostname=host) as tls:
                            if tls.selected_alpn_protocol() == "h2":
                                return host
                except OSError:
                    continue
        except OSError:
            continue
    raise SetupError("REALITY 目标的 TLS 1.3/HTTP/2 检查未通过，请检查 VPS 出站，或用 --reality-target 指定合适的域名。")


def apt(*arguments):
    logged("apt", "apt-get", "-o", "DPkg::Lock::Timeout=120", *arguments,
           timeout=900, env={**os.environ, "DEBIAN_FRONTEND": "noninteractive",
                             "NEEDRESTART_MODE": "l"})


def firewall(spec, ssh_server_port):
    active = "Status: active" in run("ufw", "status")  # LC_ALL=C
    if active or spec.enable_ufw:
        if not active:
            run("ufw", "allow", f"{ssh_server_port}/tcp")
        for rule in spec.cloud_ports():
            run("ufw", "allow", rule)
        if not active:
            run("ufw", "--force", "enable")


def obtain_release(name):
    url, expected_hash = RELEASES[name]
    archive = WORK / (name + ".archive")
    if archive.exists():
        with archive.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() == expected_hash:
                return archive
    candidate = archive.with_suffix(".download")
    logged(name + "-download", "curl", "--fail", "--silent", "--show-error", "--location",
           "--retry", "2", "--connect-timeout", "15", "--max-time", "220",
           "--proto", "=https", "--proto-redir", "=https", "-o", str(candidate), url,
           timeout=700)
    with candidate.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected_hash:
            raise SetupError(f"{name} 发行包 SHA-256 不匹配，已中止安装。")
    candidate.replace(archive)
    return archive


def install_panel(state):
    if state.get("panel_ready"):
        return
    panel_archive, core_archive = (obtain_release(name) for name in ["panel", "xray"])
    (PANEL / "bin").mkdir(mode=0o700, parents=True, exist_ok=True)
    for parent in [Path("/etc/x-ui"), Path("/var/log/x-ui")]:
        parent.mkdir(mode=0o700, exist_ok=True)
    with tarfile.open(panel_archive) as archive:
        for file in ["x-ui", "bin/geosite.dat", "bin/geoip.dat", "bin/LICENSE"]:
            member = archive.getmember("x-ui/" + file)
            if not member.isfile():
                raise SetupError("3x-ui 发行包结构与锁定版本不符。")
            (PANEL / file).write_bytes(archive.extractfile(member).read())
    with zipfile.ZipFile(core_archive) as archive:
        CORE.write_bytes(archive.read("xray"))
    (PANEL / "x-ui").chmod(0o755)
    CORE.chmod(0o755)
    if not state.get("reality_private"):
        keys = dict(line.split(": ", 1) for line in run(str(CORE), "x25519").splitlines() if ": " in line)
        state["reality_private"] = keys["PrivateKey"]
        state["reality_public"] = keys.get("Password (PublicKey)") or keys.get("Password") or keys["PublicKey"]
        save_state(state)
    os.chdir(PANEL)
    logged("panel-migrate", "./x-ui", "migrate")
    logged("panel-settings", "./x-ui", "setting", "-username", state["panel_user"],
           "-password", state["panel_password"], "-listenIP", "127.0.0.1",
           "-port", str(PANEL_PORT), "-webBasePath", state["panel_path"])
    write(UNIT / "x-ui.service", """[Unit]
Description=3x-ui panel
Wants=network-online.target
After=network-online.target
[Service]
WorkingDirectory=/usr/local/x-ui
ExecStart=/usr/local/x-ui/x-ui
Restart=on-failure
RestartSec=5s
UMask=0077
LimitNOFILE=1048576
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=full
ReadWritePaths=/etc/x-ui /var/log/x-ui /usr/local/x-ui
[Install]
WantedBy=multi-user.target
""", 0o644)
    run("systemctl", "daemon-reload")
    state["panel_ready"] = True
    save_state(state)


def install_certbot():
    if not CERTBOT.is_file():
        logged("certbot-venv", "python3", "-m", "venv", str(WORK / "certbot"))
        logged("certbot-install", str(WORK / "certbot/bin/pip"), "install",
               "--disable-pip-version-check", "certbot==5.10.0")


def issue_certificate(spec):
    if (CERT / "fullchain.pem").is_file():
        return
    if any(proto == "tcp" and port == 80 for proto, port, _ in listeners()):
        raise SetupError("TCP 80 已被本机服务占用，Certbot standalone 需要在此端口临时监听。")
    identifier = ["-d", spec.domain] if spec.domain else [
        "--preferred-profile", "shortlived", "--ip-address", spec.public_ip
    ]
    try:
        logged("certificate", str(CERTBOT), "certonly", "--standalone",
               "--cert-name", CERT_NAME, "--non-interactive", "--agree-tos",
               "--register-unsafely-without-email", *identifier)
    except SetupError as exc:
        raise SetupError(f"{exc} 若日志显示连接超时，请核对云安全组及主机的入站 TCP 80；若为域名校验，另外检查 DNS。") from None


class Panel:
    def __init__(self, state):
        self.base = f"http://127.0.0.1:{PANEL_PORT}{state['panel_path']}"
        self.client = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                     urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = None
        for _ in range(20):
            try:
                self.csrf = self.api("csrf-token")
                break
            except urllib.error.URLError:
                time.sleep(1)
        if not self.csrf:
            raise SetupError("3x-ui 的本机 API 尚未就绪，请检查 x-ui 服务。")
        self.api("login", {"username": state["panel_user"], "password": state["panel_password"]})

    def api(self, path, data=None):
        headers = {"Content-Type": "application/json"}
        if self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        req = urllib.request.Request(self.base + path, headers=headers,
                                     data=None if data is None else json.dumps(data).encode())
        with self.client.open(req, timeout=5) as response:
            result = json.load(response)
        if not result.get("success"):
            raise SetupError(f"3x-ui API {path} 拒绝了操作。请核对面板日志和当前账号设置。")
        return result.get("obj")


def inbound_specs(spec, state):
    def user(name, **fields):
        return {"email": name, "enable": True, "subId": state["sub_id"],
                "limitIp": 0, "totalGB": 0, "expiryTime": 0, **fields}

    def inbound(remark, proto, port, settings, stream, listen="0.0.0.0"):
        return {"remark": remark, "protocol": proto, "port": port, "listen": listen,
                "enable": True, "total": 0, "expiryTime": 0, "up": 0, "down": 0,
                "shareAddrStrategy": "custom", "shareAddr": spec.endpoint,
                "settings": settings, "streamSettings": stream,
                "sniffing": {"enabled": False, "destOverride": []}}

    target = state["target"]
    return [
        inbound("REALITY", "vless", spec.reality_port,
                {"decryption": "none", "clients": [user("reality", id=state["reality_uuid"], flow="xtls-rprx-vision")]},
                {"network": "tcp", "security": "reality", "realitySettings": {
                    "show": False, "xver": 0, "target": f"{target}:443", "serverNames": [target],
                    "privateKey": state["reality_private"], "shortIds": [state["short_id"]],
                    "settings": {"publicKey": state["reality_public"], "fingerprint": "chrome",
                                 "serverName": target, "spiderX": "/"},
                }}),
        inbound("VMess TLS", "vmess", WS_PORT, {"clients": [user("vmess", id=state["vmess_uuid"], alterId=0)]},
                {"network": "ws", "security": "none", "wsSettings": {"path": state["ws_path"], "headers": {}},
                 "externalProxy": [{"dest": spec.endpoint, "port": spec.https_port, "forceTls": "tls",
                                    "sni": spec.endpoint, "alpn": ["http/1.1"]}]}, "127.0.0.1"),
        inbound("Hy2", "hysteria", spec.hy2_port,
                {"version": 2, "clients": [user("hy2", auth=state["hy2_auth"])]},
                {"network": "hysteria", "security": "tls",
                 "tlsSettings": {"serverName": spec.endpoint, "minVersion": "1.3", "alpn": ["h3"],
                                 "certificates": [{"certificateFile": str(CERT / "fullchain.pem"),
                                                   "keyFile": str(CERT / "privkey.pem")}]},
                 "hysteriaSettings": {"version": 2, "udpIdleTimeout": 60},
                 "finalmask": {"udp": [{"type": "salamander", "settings": {"password": state["hy2_obfs"]}}]}}),
    ]


def configure_panel(spec, state):
    panel = Panel(state)
    settings = panel.api("panel/api/setting/all", {})
    settings.update({"subEnable": True, "subListen": "127.0.0.1", "subPort": SUB_PORT,
                     "subDomain": "", "subClashEnable": True,
                     "subClashURI": f"https://{spec.endpoint}:{spec.https_port}/mihomo/"})
    panel.api("panel/api/setting/update", settings)
    current = panel.api("panel/api/inbounds/list") or []
    for entry in inbound_specs(spec, state):
        matches = [row for row in current if row["port"] == entry["port"] and row["protocol"] == entry["protocol"]]
        if not matches:
            panel.api("panel/api/inbounds/add", entry)
            continue
        existing_settings = matches[0]["settings"]
        if isinstance(existing_settings, str):
            existing_settings = json.loads(existing_settings)
        if not any(user.get("subId") == state["sub_id"] for user in existing_settings.get("clients", [])):
            raise SetupError(f"已有其他入站占用 {entry['port']}，已保留原配置。")


def configure_nginx(spec, state):
    stock = Path("/etc/nginx/sites-enabled/default")
    installed = Path("/etc/nginx/nginx.conf").exists()
    if not installed:
        # apt 中断后重跑也应识别刚刚创建的默认站点。
        state["installed_nginx"] = True
        save_state(state)
        apt("install", "-y", "nginx")
    if state.get("installed_nginx") and stock.is_symlink() and stock.resolve() == Path("/etc/nginx/sites-available/default"):
        stock.unlink()
    write(SITE, f"""server {{
    listen {spec.https_port} ssl;
    server_name {spec.endpoint};
    server_tokens off;
    access_log off;
    error_log /var/log/nginx/vps-node-setup.log crit;
    ssl_certificate {CERT}/fullchain.pem;
    ssl_certificate_key {CERT}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    location / {{ return 404; }}
    location = {state["ws_path"]} {{
        proxy_pass http://127.0.0.1:{WS_PORT};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
        proxy_buffering off;
    }}
    location ^~ /mihomo/ {{
        proxy_pass http://127.0.0.1:{SUB_PORT};
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        add_header Cache-Control "no-store" always;
    }}
}}
""")
    if not ENABLED.is_symlink():
        ENABLED.symlink_to(SITE)
    logged("nginx-test", "nginx", "-t")
    run("systemctl", "enable", "--now", "nginx")
    run("systemctl", "reload", "nginx")


def configure_renewal():
    hook = WORK / "renew-hook"
    write(hook, "#!/bin/sh\nset -eu\n/usr/sbin/nginx -t\nsystemctl reload nginx\nsystemctl restart x-ui\n", 0o700)
    write(UNIT / "vps-node-renew.service", f"""[Unit]
Description=Renew the VPS proxy TLS certificate
Wants=network-online.target
After=network-online.target
[Service]
Type=oneshot
UMask=0077
ExecStart={CERTBOT} renew --quiet --cert-name {CERT_NAME} --no-random-sleep-on-renew --deploy-hook {hook}
""", 0o644)
    write(UNIT / "vps-node-renew.timer", """[Unit]
Description=Check VPS certificate twice daily
[Timer]
OnCalendar=*-*-* 00,12:15:00
RandomizedDelaySec=1800
Persistent=true
[Install]
WantedBy=timers.target
""", 0o644)
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", "vps-node-renew.timer")


def export(spec, state):
    import yaml

    run("systemctl", "is-active", "x-ui", "nginx", "vps-node-renew.timer")
    logged("xray-test", str(CORE), "run", "-test", "-config", str(PANEL / "bin/config.json"))
    panel = Panel(state)
    rows = panel.api("panel/api/inbounds/list") or []
    expected_inbounds = {(item["protocol"], item["port"]) for item in inbound_specs(spec, state)}
    if not expected_inbounds <= {(row["protocol"], row["port"]) for row in rows if row["enable"]}:
        raise SetupError("面板没有返回三个已启用的入站。")
    required = spec.sockets() - {("tcp", 80)}
    for _ in range(12):
        bound = listeners()
        if required <= {(proto, port) for proto, port, _ in bound}:
            break
        time.sleep(1)
    else:
        raise SetupError("仍有服务端口未监听，请检查 x-ui 和 nginx。")
    if any(port in [WS_PORT, PANEL_PORT, SUB_PORT] and host != "127.0.0.1"
           for proto, port, host in bound if proto == "tcp"):
        raise SetupError("发现内部端口未绑定到回环地址。")
    if any(proto == "tcp" and port == 80 for proto, port, _ in bound):
        raise SetupError("发现 TCP 80 有常驻监听，可能阻塞证书自动续期。")
    sub_path = "/mihomo/" + state["sub_id"]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(f"http://127.0.0.1:{SUB_PORT}{sub_path}", headers={
        "User-Agent": "Mihomo/1.19.33", "Host": spec.endpoint,
    })
    with opener.open(request, timeout=10) as response:
        raw = response.read(512 * 1024).decode("utf-8")
    config = yaml.safe_load(raw)
    ports = {"vless": spec.reality_port, "vmess": spec.https_port, "hysteria2": spec.hy2_port}
    nodes = config["proxies"]
    if (len(nodes) != 3 or {node["type"]: node["port"] for node in nodes} != ports
            or not config.get("proxy-groups") or not config.get("rules")
            or any(node["server"] != spec.endpoint or node.get("skip-cert-verify") for node in nodes)
            or not next(node for node in nodes if node["type"] == "vless")["reality-opts"].get("support-x25519mlkem768")):
        raise SetupError("导出的订阅缺少节点、策略组或必要的 TLS 参数。")
    return {
        "status": {"public_ip": spec.public_ip, "endpoint": spec.endpoint, "ports": ports,
                   "reality_target": state["target"], "services": "active",
                   "certificate_not_after": run("openssl", "x509", "-in", str(CERT / "fullchain.pem"),
                                               "-noout", "-enddate").strip().removeprefix("notAfter=")},
        "panel": {key: state[key] for key in ["panel_path", "panel_user", "panel_password"]} | {"panel_port": PANEL_PORT},
        "subscription": f"https://{spec.endpoint}:{spec.https_port}{sub_path}",
        "mihomo_yaml": raw,
        "mihomo_config": config,
    }


def deploy(spec, ssh_server_port):
    import fcntl

    WORK.mkdir(mode=0o700, exist_ok=True)
    with (WORK / "deploy.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError("已有部署在运行，请等它结束后再试。") from None
        state = read_state(spec)
        preflight(spec, ssh_server_port, state)
        if state and state.get("complete"):
            return export(spec, state)
        if not state:
            note("检查 REALITY 目标的 TLS 1.3 和 HTTP/2")
            state = {"spec": asdict(spec), "target": find_target(spec),
                     "panel_user": "owner_" + secrets.token_hex(4), "panel_password": secrets.token_urlsafe(24),
                     "panel_path": "/" + secrets.token_urlsafe(18) + "/", "sub_id": secrets.token_hex(16),
                     "ws_path": "/" + secrets.token_urlsafe(24), "reality_uuid": str(uuid.uuid4()),
                     "vmess_uuid": str(uuid.uuid4()), "short_id": secrets.token_hex(8),
                     "hy2_auth": secrets.token_urlsafe(24), "hy2_obfs": secrets.token_urlsafe(24)}
            save_state(state)
        if not state.get("prerequisites_ready"):
            note("安装系统依赖，保留现有 SSH 设置")
            apt("update")
            apt("install", "-y", "ca-certificates", "curl", "openssl", "python3-venv", "python3-yaml", "ufw")
            state["prerequisites_ready"] = True
            save_state(state)
        firewall(spec, ssh_server_port)
        note("申请或复用 TLS 证书")
        install_certbot()
        issue_certificate(spec)
        note("安装锁定版本，配置 3x-ui 节点和 HTTPS 订阅")
        install_panel(state)
        run("systemctl", "enable", "--now", "x-ui")
        configure_panel(spec, state)
        run("systemctl", "restart", "x-ui")
        configure_nginx(spec, state)
        configure_renewal()
        result = export(spec, state)
        with sqlite3.connect("file:/etc/x-ui/x-ui.db?mode=ro", uri=True) as source:
            with sqlite3.connect(WORK / "x-ui-initial.db") as backup:
                source.backup(backup)
        state["complete"] = True
        save_state(state)
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["preflight", "deploy", "renewal-test"])
    parser.add_argument("ssh_connection")
    parser.add_argument("options")
    args = parser.parse_args()
    os.umask(0o077)
    os.environ.update(LC_ALL="C", LANG="C")
    if os.geteuid() != 0:
        raise SetupError("远端需要 root 或免交互 sudo。")
    spec = Spec(**json.loads(args.options))
    spec.check()
    connection = args.ssh_connection.split()
    if len(connection) != 4 or not connection[3].isdigit():
        raise SetupError("无法识别本次 SSH 的服务端端口。")
    ssh_server_port = int(connection[3])
    state = read_state(spec)
    if args.action == "preflight":
        result = preflight(spec, ssh_server_port, state)
    elif args.action == "renewal-test":
        if not state or not state.get("complete"):
            raise SetupError("请先完成部署再检查续期。")
        logged("renewal-test", str(CERTBOT), "renew", "--dry-run", "--cert-name",
               CERT_NAME, "--no-random-sleep-on-renew", timeout=600)
        result = {"renewal_dry_run": "passed"}
    else:
        result = deploy(spec, ssh_server_port)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    try:
        main()
    except (SetupError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        note(str(exc) if isinstance(exc, SetupError) else f"部署中止（{type(exc).__name__}），请检查上一阶段和服务器受限日志。")
        raise SystemExit(1) from None
