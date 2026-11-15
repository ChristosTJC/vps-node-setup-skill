"""通过已验证的 SSH 连接部署 VPS 节点，交付本机私有连接资料。"""

import argparse
import base64
from dataclasses import asdict
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

from open_panel import ssh_args
from server import SetupError, Spec
from verify import check_subscription


SCRIPTS = Path(__file__).resolve().parent
CLIENT_HOME = Path.home() / ".vps-node-setup"


def options(args):
    public_ip = ipaddress.IPv4Address(args.public_ip or args.host)
    if not public_ip.is_global:
        raise SetupError("请填写 VPS 的公网 IPv4；SSH 使用其他地址时，通过 --public-ip 指定公网 IPv4。")
    spec = Spec(str(public_ip), args.domain.lower().rstrip("."), args.reality_target.lower().rstrip("."),
                args.reality_port, args.https_port, args.hy2_port, args.enable_ufw)
    spec.check()
    if ("tcp", args.port) in spec.sockets():
        raise SetupError("SSH 公网端口与应用端口冲突。")
    return spec


def ssh_access(args):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}", args.host):
        raise SetupError("SSH 目标应为 IPv4 或主机名。")
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_.-]{0,31}", args.user):
        raise SetupError("SSH 用户名格式不正确。")
    if not 1 <= args.port <= 65535:
        raise SetupError("SSH 端口必须在 1–65535 之间。")
    access = {"ssh_host": args.host, "ssh_port": args.port, "ssh_user": args.user}
    for field in ["identity", "known_hosts"]:
        path = getattr(args, field)
        if path:
            path = path.expanduser().resolve(strict=True)
            if not path.is_file() or '"' in str(path) or "\n" in str(path):
                raise SetupError("请提供有效的 SSH 密钥和 known_hosts 文件路径。")
            access[field] = str(path)
    return access


def run_remote(args, access, spec):
    # 只有最后的 SSH_CONNECTION 片段由远端 shell 展开。其他参数均逐个引用。
    interpreter = "python3" if access["ssh_user"] == "root" else "sudo -n -- python3"
    encoded = json.dumps(asdict(spec), separators=(",", ":"))
    command = f'exec {interpreter} - {shlex.quote(args.command)} "$SSH_CONNECTION" {shlex.quote(encoded)}'
    result = subprocess.run(
        ssh_args(access) + ["-T", access["ssh_host"], command],
        input=(SCRIPTS / "server.py").read_bytes(), stdout=subprocess.PIPE, timeout=3600,
    )
    if result.returncode:
        raise SetupError("远端未完成。根据上方错误处理后可用原参数重跑。已有服务的凭据不会重新生成。")
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise SetupError("远端响应无法解析，请检查 SSH 登录脚本是否向标准输出写入内容。") from None


def ensure_private(directory):
    if os.name != "nt":
        directory.chmod(0o700)
        return
    # 仅限制本 Skill 专用的交付目录，不改变其他工作区的权限。
    script = """
$ErrorActionPreference = 'Stop'
$path = $env:VPS_NODE_PRIVATE_DIRECTORY
$current = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$acl = [IO.Directory]::GetAccessControl($path)
$acl.SetSecurityDescriptorSddlForm("D:P(A;OICI;FA;;;$current)", [Security.AccessControl.AccessControlSections]::Access)
[IO.Directory]::SetAccessControl($path, $acl)
$actual = [IO.Directory]::GetAccessControl($path)
$rules = $actual.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])
if (!$actual.AreAccessRulesProtected -or $rules.Count -ne 1 -or $rules[0].IdentityReference.Value -ne $current) {
    throw 'Unexpected ACL'
}
"""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                   env={**os.environ, "VPS_NODE_PRIVATE_DIRECTORY": str(directory)},
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=15)


def reserve_bundle(args, spec):
    path = (args.bundle or CLIENT_HOME / spec.public_ip).expanduser().resolve()
    if any((parent / ".git").exists() for parent in (path, *path.parents)):
        raise SetupError("交付目录含账号和订阅，请指定 Git 仓库以外的专用目录。")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = path / ".vps-node-setup"
    if marker.exists():
        if marker.read_text(encoding="utf-8").strip() != spec.public_ip:
            raise SetupError("该交付目录属于另一台 VPS，请指定新目录。")
    elif any(path.iterdir()):
        raise SetupError("交付目录已有其他文件，请指定专用空目录。")
    ensure_private(path)
    put(path, marker.name, spec.public_ip + "\n")
    return path


def put(directory, name, content):
    descriptor, pending = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
        os.replace(pending, directory / name)
    finally:
        Path(pending).unlink(missing_ok=True)


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def save_bundle(path, result, access):
    panel = result["panel"]
    put(path, "connection.json", json_text(access | panel))
    put(path, "panel-login.txt", f"3x-ui 面板用户名：{panel['panel_user']}\n密码：{panel['panel_password']}\n"
        "Windows 双击 open-panel.cmd。macOS/Linux 运行 python3 open_panel.py。\n"
        "面板连接使用原 SSH 密钥及主机指纹，本目录不包含 SSH 私钥。\n")
    put(path, "open_panel.py", (SCRIPTS / "open_panel.py").read_text(encoding="utf-8"))
    put(path, "open-panel.cmd", '@echo off\r\ncd /d "%~dp0"\r\n'
        'where py.exe >nul 2>&1\r\nif errorlevel 1 (python open_panel.py) else (py -3 open_panel.py)\r\n'
        'if errorlevel 1 pause\r\n')
    put(path, "subscription.txt", result["subscription"] + "\n")
    put(path, "mihomo.yaml", result["mihomo_yaml"])
    put(path, "mihomo-config.json", json_text(result["mihomo_config"]))
    put(path, "status.json", json_text(result["status"]))


def authorize_key(args, access):
    if not args.pubkey:
        raise SetupError("通过 --pubkey 指定要添加的 Ed25519 公钥文件。")
    key = args.pubkey.expanduser().read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]+)?", key):
        raise SetupError("需要单行 Ed25519 公钥文件（.pub）。")
    command = (
        'set -eu; umask 077; mkdir -p "$HOME/.ssh"; chmod 700 "$HOME/.ssh"; '
        'file="$HOME/.ssh/authorized_keys"; touch "$file"; chmod 600 "$file"; '
        'IFS= read -r key; grep -Fxq -- "$key" "$file" || printf "\\n%s\\n" "$key" >> "$file"'
    )
    print("如 SSH 提示输入密码，请在此终端输入服务商提供的密码。", flush=True)
    result = subprocess.run(ssh_args(access, interactive=True) + ["-T", access["ssh_host"], command],
                            input=(key + "\n").encode(), timeout=180)
    if result.returncode:
        raise SetupError("未能追加公钥。可以通过服务商控制台追加 .pub 文件后重试。")
    print("公钥已添加。请带对应 --identity 运行 preflight 验证。")
    return 0


def parser():
    top = argparse.ArgumentParser(description=__doc__)
    base = argparse.ArgumentParser(add_help=False)
    base.add_argument("--host", required=True, help="SSH 地址")
    base.add_argument("--port", type=int, default=22, help="现有 SSH 公网端口")
    base.add_argument("--user", default="root", help="root 或已配置免交互 sudo 的账号")
    base.add_argument("--identity", type=Path, help="可选，本机 SSH 私钥文件")
    base.add_argument("--known-hosts", type=Path, help="可选，已核对的主机指纹文件")
    deploy_args = argparse.ArgumentParser(add_help=False, parents=[base])
    deploy_args.add_argument("--public-ip", help="公网 IPv4；SSH 地址不是该 IPv4 时填写")
    deploy_args.add_argument("--domain", default="", help="可选，直接解析到 VPS 公网 IPv4 的域名")
    deploy_args.add_argument("--reality-target", default="", help="可选，验证 TLS 1.3 和 HTTP/2 的目标域名")
    deploy_args.add_argument("--reality-port", type=int, default=443)
    deploy_args.add_argument("--https-port", type=int, default=33417, help="VMess 与 Mihomo 订阅的 TCP 端口")
    deploy_args.add_argument("--hy2-port", type=int, default=46289)
    deploy_args.add_argument("--enable-ufw", action="store_true", help="在确认是干净 VPS 时，配置并启用 UFW")
    commands = top.add_subparsers(dest="command", required=True)
    key = commands.add_parser("authorize-key", parents=[base], help="通过终端 SSH 登录并追加一把公钥")
    key.add_argument("--pubkey", type=Path, required=True)
    commands.add_parser("preflight", parents=[deploy_args], help="只读检查服务器和端口")
    deploy = commands.add_parser("deploy", parents=[deploy_args], help="部署并导出连接资料，重复运行可恢复中断")
    deploy.add_argument("--bundle", type=Path, help="Git 仓库以外用于保存连接资料的专用目录")
    deploy.add_argument("--mihomo", type=Path, help="可选，使用指定 Mihomo 实测；省略时试用 PATH 已安装的 Mihomo")
    deploy.add_argument("--interface", help="可选，实测时绑定物理网卡名称")
    commands.add_parser("renewal-test", parents=[deploy_args], help="用 ACME 测试环境模拟续期")
    return top


def main():
    args = parser().parse_args()
    access = ssh_access(args)
    if args.command == "authorize-key":
        return authorize_key(args, access)
    spec = options(args)
    if args.command != "deploy":
        print(json_text(run_remote(args, access, spec)), end="")
        return 0
    bundle = reserve_bundle(args, spec)
    ports = [f"{args.port}/tcp (SSH)", *spec.cloud_ports()]
    print("核对云安全组入站规则：" + ", ".join(ports), flush=True)
    result = run_remote(args, access, spec)
    save_bundle(bundle, result, access)
    print(f"服务端检查通过，连接资料已保存到 {bundle}", flush=True)
    mihomo = args.mihomo or shutil.which("mihomo") or shutil.which("verge-mihomo")
    if mihomo:
        command = [sys.executable, str(SCRIPTS / "verify.py"), "--bundle", str(bundle), "--mihomo", str(mihomo)]
        if args.interface:
            command += ["--interface", args.interface]
        return subprocess.run(command).returncode
    subscription = check_subscription(result["subscription"])
    put(bundle, "verification.json", json_text({"subscription": subscription, "node_egress_ok": None,
                                                "local_error": "mihomo_not_found"}))
    print("公网 HTTPS 订阅：" + ("通过" if subscription["ok"] else "未通过"))
    print("本机未找到 Mihomo。节点实连可在客户端导入后验证，或运行 verify.py --mihomo。")
    return 0 if subscription["ok"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SetupError, ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        message = str(exc) if isinstance(exc, SetupError) else f"操作中止（{type(exc).__name__}），请检查参数、连接和文件权限。"
        print(message, file=sys.stderr)
        raise SystemExit(1) from None
