# 操作与排查

## 最少输入与初次登录

需要一台用户有权管理的 Ubuntu 24.04 LTS x86_64 VPS、一个可接收入站的公网 IPv4，以及现有 SSH 入口。Linux 客户端也可以 SSH 到不同地址，再通过 `--public-ip` 指定节点的公网 IPv4。Windows/macOS/Linux 本机需 Python 3.10+ 和 OpenSSH。

先用服务商控制台或开通资料对照 SSH 主机指纹，再用 `ssh -p <端口> <账号>@<IP>` 初次连接并保存匹配的指纹。控制台可用时，在 VPS 上运行 `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` 查看服务器 ED25519 指纹。主机重装后指纹变化，应通过可信控制台复核新指纹。SSH 客户端使用系统 `known_hosts` 或带 `--known-hosts` 的指定文件。

本机没有专用密钥时，用 `ssh-keygen -t ed25519 -f <本机新私钥路径>` 创建。将 `.pub` 的单行公钥放入目标账号的 `~/.ssh/authorized_keys`，可通过服务商控制台或 `deploy.py authorize-key --host <IP> --port <端口> --user <账号> --pubkey <公钥文件>` 完成。后者由 OpenSSH 在本机终端提示密码，下载或发布的文件里不包含它。密码登录已关闭且没有私钥时，用服务商控制台追加公钥。完成后用 `preflight` 复验。

SSH 命令使用显式主机、端口、私钥和主机记录，便于跨系统复现。若现有连接依赖跳板机、自定义协商选项或需要输入密钥口令，先准备直达连接或将已解锁的密钥加载到本机 SSH agent。

## 网络入口

同时检查服务商安全组、VPS 防火墙和实际监听。保留现有 SSH 规则。公网放行证书验证 TCP 80、REALITY TCP 443、VMess 与订阅 TCP 33417、Hy2 UDP 46289，或使用部署前检查返回的自定义端口。云安全组表单中，单个端口的最小和最大端口填同一个值。

脚本在启用的 UFW 上添加应用端口规则。使用 `--enable-ufw` 时，在启用前先允许本次 SSH 使用的服务端端口。其他防火墙由用户管理。默认部署从新 VPS 开始。发现已有面板、冲突端口、未管理的 Nginx 默认站点或同名资源时先协调迁移。

## 证书与续期

无域名使用 Certbot 的 Let’s Encrypt 短有效期 IP 证书，证书名是 `vps-node-setup`；有域名使用 DNS A 记录直接指向 VPS 的域名证书。公网 TCP 80 需持续允许 ACME 请求，Certbot 只在签发或续期期间临时监听；Nginx 常驻 HTTPS 端口。当前实现保留入站 80 的允许规则，以兼容既有 UFW，避免续期后误删用户原有规则。

核对状态和模拟续期：

```sh
systemctl list-timers vps-node-renew.timer
sudo journalctl -u vps-node-renew.service -n 30 --no-pager
python .agents/skills/vps-node-setup/scripts/deploy.py renewal-test --host VPS_IP --port SSH_PORT --identity KEY_PATH
```

`renewal-test` 复用部署参数（包括可选域名和自定义端口），向测试环境申请并验证 HTTP-01，保留现有信任证书。正式续期成功后重载 Nginx、重启 x-ui，让 Hy2 使用新证书。日志目录是服务器上的 `/var/lib/vps-node-setup/`，查看证书签发文件 `certificate.log` 判断是云端口、DNS、CA 限额还是其他原因。

## 验证和恢复

- 服务器侧对照 `ss -lntu`、`systemctl status x-ui nginx` 与 `nginx -t`，再检查订阅是否给出 3 个正确协议及端口。公网 HTTPS 成功后在客户端实际使用的网络用 `verify.py` 逐个连通并核对出口 IP。Cloudflare 校验与 Google 校验分开看。
- 三种节点只有延迟测试全超时，先换健康检查目标，再核实服务端 DNS、出口到目标站的 TCP 和客户端到 VPS 的 TCP/UDP。跨运营商或换热点可作对照。需要抓包时只记录相关回合的包头和连接方向，避免收集请求正文。
- 学校或公司网络带宽波动，固定测试目标、并发、方向和时间窗，对照本机直连、VPS 本机出口、三种节点及其他网络。到网关的 ping 无法定位上游带宽或回程。
- 客户端导入要求支持 REALITY 和 Hy2 的较新 Mihomo。旧 Clash 内核可能拒绝某些节点。使用原生 `/mihomo/` 路径的 YAML。有的客户端会覆写策略或根据当前代理更新订阅，必要时导入本地 `mihomo.yaml` 验证。
- Windows 的 Clash Verge 正在运行时，可用 `Get-Process -Name verge-mihomo,mihomo -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Path` 找到内核可执行文件，交给 `--mihomo`。
- 遇到部分安装失败时保留原参数并重跑。服务端状态和初始面板数据库位于 root 可读的 `/var/lib/vps-node-setup/`。安装成功后再次运行部署命令会读取受控服务的状态并重新导出，没有恢复出厂动作。若已在面板轮换账户或订阅，按现有设置交接，避免用初始凭据覆盖修改。
- 系统大版本升级、重装、改变 SSH 安全策略和改动现用 Clash 全局配置都有独立影响，按用户实际需求安排。在要求验证重启时，先确认能用当前密钥创建新的 SSH 连接，再核对重启后的系统内核、服务与续期任务。
