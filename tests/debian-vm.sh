#!/usr/bin/env bash
set -euo pipefail
if [[ "${GITHUB_ACTIONS:-}" != true ]]; then
    echo '从 GitHub Actions 启动临时 Debian 虚拟机。' >&2
    exit 2
fi

lab="$(mktemp -d "$RUNNER_TEMP/vps-debian.XXXXXX")"
cd "$lab"
sudo apt-get -qq update
sudo apt-get -qq install -y --no-install-recommends qemu-system-x86 cloud-image-utils
source_url='https://cloud.debian.org/images/cloud/trixie/latest'
image='debian-13-genericcloud-amd64.qcow2'
curl -fsSL --retry 3 --max-time 180 -o "$image" "$source_url/$image"
curl -fsSL --retry 3 --max-time 45 -o SHA512SUMS "$source_url/SHA512SUMS"
awk -v image="$image" '$2 == image { print }' SHA512SUMS | sha512sum --check -
qemu-img create -f qcow2 -F qcow2 -b "$lab/$image" guest.qcow2 8G
ssh-keygen -qt ed25519 -N '' -f identity
ssh-keygen -qt ed25519 -N '' -f host
printf '[127.0.0.1]:13222 %s\n' "$(cut -d' ' -f1-2 host.pub)" > known_hosts
cat > user-data <<EOF
#cloud-config
users:
  - name: tester
    groups: [sudo]
    sudo: ["ALL=(ALL) NOPASSWD:ALL"]
    shell: /bin/bash
    ssh_authorized_keys:
      - $(cat identity.pub)
ssh_deletekeys: false
ssh_keys:
  ed25519_private: |
$(sed 's/^/    /' host)
  ed25519_public: $(cat host.pub)
EOF
printf 'instance-id: vps-node-test\nlocal-hostname: debian-test\n' > meta-data
cloud-localds seed.img user-data meta-data

cpu=(-accel tcg,thread=multi -cpu max)
if [[ -r /dev/kvm && -w /dev/kvm ]]; then
    cpu=(-accel kvm -cpu host)
fi
qemu-system-x86_64 "${cpu[@]}" -smp 2 -m 2048 -display none -daemonize \
    -pidfile "$lab/qemu.pid" -serial "file:$lab/serial.log" \
    -drive file=guest.qcow2,format=qcow2,if=virtio \
    -drive file=seed.img,format=raw,if=virtio,readonly=on \
    -nic user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:13222-:22
trap 'kill "$(cat "$lab/qemu.pid")" 2>/dev/null || true' EXIT
remote=(ssh -F none -p 13222 -i "$lab/identity" -o BatchMode=yes -o ConnectTimeout=4
        -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$lab/known_hosts" tester@127.0.0.1)
for attempt in {1..90}; do
    if "${remote[@]}" true 2>/dev/null; then
        break
    fi
    sleep 2
done
if ! "${remote[@]}" 'timeout 120 cloud-init status --wait'; then
    tail -n 30 "$lab/serial.log"
    exit 1
fi
git -C "$GITHUB_WORKSPACE" archive HEAD | "${remote[@]}" 'mkdir -p ~/source && tar -xf - -C ~/source'
"${remote[@]}" 'sudo apt-get -qq update && sudo env DEBIAN_FRONTEND=noninteractive apt-get -qq install -y curl ca-certificates ufw &&
    sudo ufw allow 22/tcp &&
    cd ~/source && GITHUB_ACTIONS=true RUNNER_TEMP="$HOME" VPS_SMOKE_ACME=pebble bash tests/smoke.sh'
