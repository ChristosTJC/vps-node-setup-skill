"""在临时虚拟机上运行测试 ACME，并验收生产续期单元。"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request

import server


TRUST = Path("/etc/ssl/certs/ca-certificates.crt")
DIRECTORY = "https://127.0.0.1:14000/dir"
CLI_CONFIG = Path("/etc/letsencrypt/cli.ini")


@contextmanager
def test_ca(directory):
    listener_cert = directory / "acme.crt"
    listener_key = directory / "acme.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=CI ACME", "-addext", "subjectAltName=IP:127.0.0.1",
                    "-keyout", str(listener_key), "-out", str(listener_cert)], check=True, capture_output=True)
    config = directory / "pebble.json"
    config.write_text(json.dumps({"pebble": {
        "listenAddress": "127.0.0.1:14000", "managementListenAddress": "127.0.0.1:15000",
        "certificate": str(listener_cert), "privateKey": str(listener_key), "httpPort": 80, "tlsPort": 443,
        "profiles": {"shortlived": {"description": "CI 短期证书", "validityPeriod": 518400}},
    }}), encoding="utf-8")
    with (directory / "pebble.log").open("wb") as log:
        process = subprocess.Popen([str(directory / "pebble"), "-config", str(config)], stdout=log,
                                   stderr=subprocess.STDOUT, env={**os.environ, "PEBBLE_VA_NOSLEEP": "1",
                                   "PEBBLE_WFE_NONCEREJECT": "0", "PEBBLE_AUTHZREUSE": "0"})
        try:
            context = ssl.create_default_context(cafile=str(listener_cert))
            for _ in range(40):
                try:
                    with urllib.request.urlopen(DIRECTORY, context=context, timeout=1) as response:
                        assert response.status == 200
                    break
                except urllib.error.URLError:
                    if process.poll() is not None:
                        raise RuntimeError("Pebble 启动失败")
                    time.sleep(0.25)
            else:
                raise RuntimeError("Pebble 服务未就绪")
            with urllib.request.urlopen("https://127.0.0.1:15000/roots/0", context=context, timeout=5) as response:
                root_cert = response.read()
            trusted = Path("/usr/local/share/ca-certificates")
            (trusted / "vps-smoke-root.crt").write_bytes(root_cert)
            (trusted / "vps-smoke-acme.crt").write_bytes(listener_cert.read_bytes())
            subprocess.run(["update-ca-certificates"], check=True, capture_output=True)
            CLI_CONFIG.parent.mkdir(parents=True, exist_ok=True)
            CLI_CONFIG.write_text(f"server = {DIRECTORY}\n", encoding="utf-8")
            os.environ["REQUESTS_CA_BUNDLE"] = str(TRUST)
            yield
        except Exception:
            for path in [directory / "pebble.log", server.WORK / "certificate.log"]:
                if path.exists():
                    print(path.read_text(errors="replace")[-5000:], flush=True)
            raise
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def certificate_der():
    return subprocess.check_output(["openssl", "x509", "-in", str(server.CERT / "fullchain.pem"),
                                    "-outform", "DER"])


def verify_renewal(spec):
    previous = certificate_der()
    panel_pid = server.run("systemctl", "show", "-p", "MainPID", "--value", "x-ui")
    dropin = server.UNIT / "vps-node-renew.service.d"
    dropin.mkdir()
    (dropin / "ci-trust.conf").write_text(f"[Service]\nEnvironment=REQUESTS_CA_BUNDLE={TRUST}\n", encoding="utf-8")
    # 测试证书刚签发。让这一次在立即到期的条件下执行。
    CLI_CONFIG.write_text(f"server = {DIRECTORY}\nforce-renewal = true\n", encoding="utf-8")
    server.run("systemctl", "daemon-reload")
    result = subprocess.run(["systemctl", "start", "vps-node-renew.service"], timeout=120)
    if result.returncode:
        subprocess.run(["journalctl", "-u", "vps-node-renew.service", "-n", "30", "--no-pager"], check=False)
        result.check_returncode()
    current = certificate_der()
    assert current != previous, "Certbot 未更新证书"
    assert server.run("systemctl", "show", "-p", "MainPID", "--value", "x-ui") != panel_pid
    context = ssl.create_default_context()
    for _ in range(20):
        with socket.create_connection((spec.public_ip, spec.https_port), timeout=3) as sock:
            with context.wrap_socket(sock, server_hostname=spec.public_ip) as tls:
                if tls.getpeercert(binary_form=True) == current:
                    break
        time.sleep(0.25)
    else:
        raise AssertionError("Nginx 未加载续期后的证书")
    print("ACME HTTP-01 签发、续期单元、证书替换及服务重载通过", flush=True)
