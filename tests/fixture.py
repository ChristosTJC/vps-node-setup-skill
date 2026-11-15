"""在临时 Ubuntu runner 上用测试 CA 代替真实 ACME。"""

from dataclasses import asdict
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import server


def sign_test_certificate(spec, directory):
    server.CERT.mkdir(parents=True)
    issuer = directory / "smoke-ca.crt"
    issuer_key = directory / "smoke-ca.key"
    csr = directory / "smoke.csr"
    extensions = directory / "smoke.ext"
    extensions.write_text(f"subjectAltName=IP:{spec.public_ip}\nextendedKeyUsage=serverAuth\n")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=VPS Skill Smoke CA", "-keyout", str(issuer_key), "-out", str(issuer)],
                   check=True, capture_output=True)
    subprocess.run(["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=VPS Skill Smoke",
                    "-out", str(csr), "-keyout", str(server.CERT / "privkey.pem")], check=True, capture_output=True)
    subprocess.run(["openssl", "x509", "-req", "-in", str(csr), "-CA", str(issuer), "-CAkey", str(issuer_key),
                    "-CAcreateserial", "-days", "1", "-extfile", str(extensions),
                    "-out", str(server.CERT / "fullchain.pem")], check=True, capture_output=True)
    subprocess.run(["install", "-m", "644", str(issuer),
                    "/usr/local/share/ca-certificates/vps-skill-smoke.crt"], check=True)
    subprocess.run(["update-ca-certificates"], check=True, capture_output=True)


def main():
    os.umask(0o077)
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.geteuid() != 0:
        raise SystemExit("只在 GitHub Actions 的临时 Ubuntu runner 上运行。")
    spec = server.Spec(sys.argv[1], reality_port=14443, https_port=13417, hy2_port=16289)
    directory = Path(sys.argv[2])
    spec.check()
    with patch.object(server, "issue_certificate", side_effect=server.SetupError("模拟证书验证未通过")):
        try:
            server.deploy(spec, 13164)
        except server.SetupError as error:
            if str(error) != "模拟证书验证未通过":
                raise
        else:
            raise AssertionError("应在证书阶段暂停")
    initial = server.read_state(spec)
    assert initial["spec"] == asdict(spec) and not initial.get("complete")
    with patch.object(server, "issue_certificate", side_effect=lambda spec: sign_test_certificate(spec, directory)):
        result = server.deploy(spec, 13164)
    assert result["panel"]["panel_password"] == initial["panel_password"]
    assert server.STATE.stat().st_mode & 0o777 == 0o600
    assert server.read_state(spec)["complete"]
    print("服务端断点恢复、实际入站、订阅及凭据权限通过")


if __name__ == "__main__":
    main()
