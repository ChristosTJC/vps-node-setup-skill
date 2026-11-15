#!/usr/bin/env bash
set -euo pipefail
if [[ "${GITHUB_ACTIONS:-}" != true ]]; then
    echo '此测试需要 GitHub Actions 的临时 Ubuntu runner。' >&2
    exit 2
fi

scripts="$PWD/.agents/skills/vps-node-setup/scripts"
lab="$(mktemp -d "$RUNNER_TEMP/vps-skill.XXXXXX")"
ip="$(curl -4fsS --max-time 15 --retry 2 https://api.ipify.org/)"
python3 -c 'from ipaddress import IPv4Address; import sys; assert IPv4Address(sys.argv[1]).is_global' "$ip"
sudo ip addr add "$ip/32" dev lo

# 临时 runner 可能预装 nginx，此夹具只保留 Skill 的站点。
if [[ -L /etc/nginx/sites-enabled/default ]]; then
    sudo systemctl stop nginx
    sudo unlink /etc/nginx/sites-enabled/default
fi
ssh-keygen -q -t ed25519 -N '' -f "$lab/identity"
ssh-keygen -q -t ed25519 -N '' -f "$lab/host"
printf '[127.0.0.1]:13164 %s\n' "$(cut -d ' ' -f1-2 "$lab/host.pub")" > "$lab/known_hosts"
cat > "$lab/sshd_config" <<EOF
Port 13164
ListenAddress 127.0.0.1
HostKey $lab/host
PidFile $lab/sshd.pid
AuthorizedKeysFile $lab/identity.pub
UsePAM yes
PasswordAuthentication no
KbdInteractiveAuthentication no
AllowUsers $USER
EOF
sudo install -d -m 755 /run/sshd
sudo /usr/sbin/sshd -f "$lab/sshd_config"
args=(--host 127.0.0.1 --port 13164 --user "$USER" --identity "$lab/identity"
      --known-hosts "$lab/known_hosts" --public-ip "$ip"
      --reality-port 14443 --https-port 13417 --hy2-port 16289)
python3 "$scripts/deploy.py" preflight "${args[@]}"
sudo env GITHUB_ACTIONS=true PYTHONPATH="$scripts" LC_ALL=C python3 tests/fixture.py "$ip" "$lab"

curl -fsSL --max-time 90 --retry 2 -o "$lab/mihomo.gz" \
    'https://github.com/MetaCubeX/mihomo/releases/download/v1.19.33/mihomo-linux-amd64-v1-v1.19.33.gz'
printf '%s  %s\n' '8ec92e7b2fdc6b853ba00156bab7e4e965c8b0d843b8c4ac8ab98795f02ad524' "$lab/mihomo.gz" | sha256sum -c -
gzip -dc "$lab/mihomo.gz" > "$lab/mihomo"
chmod 700 "$lab/mihomo"
python3 "$scripts/deploy.py" deploy "${args[@]}" --bundle "$lab/bundle" --mihomo "$lab/mihomo"

rc=0
timeout --signal=INT 8s python3 "$lab/bundle/open_panel.py" --no-browser > "$lab/panel-check.log" || rc=$?
test "$rc" = 124
grep -q '面板通道已连接' "$lab/panel-check.log"
test "$(stat -c '%a' "$lab/bundle")" = 700
test "$(stat -c '%a' "$lab/bundle/subscription.txt")" = 600
echo '通过真实 SSH 命令导出、使用 Mihomo 实连三节点、验证私有面板通道'
