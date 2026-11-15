---
name: vps-node-setup
description: 在用户授权的 Ubuntu 24.04 VPS 上通过 SSH 部署个人代理节点。配置 3x-ui、VLESS REALITY、VMess TLS、Hysteria 2、自动续期和 Clash/Mihomo 订阅，也适用于同一套部署的恢复和连通诊断。
---

# 部署个人 VPS 节点

使用 [scripts/deploy.py](scripts/deploy.py) 完成部署。脚本从本机经 OpenSSH 部署到 Ubuntu 24.04 x86_64，可重复使用同一组参数恢复中断，并交付配置、订阅和私有面板入口。

1. 从用户和现有 SSH 配置取得 VPS 公网 IPv4、SSH 公网端口及账号。远端使用 root 或免交互 sudo。本机将私钥交给 OpenSSH 使用，先通过服务商控制台核对 SSH 主机指纹。还没有密钥时，帮助用户在本机生成专用 Ed25519 密钥，只把公钥加到服务器。可以用 `authorize-key` 让用户在可信终端输入初始密码。
2. 运行 `python scripts/deploy.py preflight --host <IP> --port <SSH端口> --user <账号> --identity <本地私钥>`。路径以此 Skill 为基准。默认应用端口是 **TCP 443（REALITY）、TCP 33417（VMess 和订阅）、UDP 46289（Hy2）、TCP 80（证书验证）**，SSH 原样沿用。用 `--reality-port`、`--https-port`、`--hy2-port` 响应用户指定的端口；TCP 和 UDP 规则分开添加。只在用户已授权且有服务商接口时修改云规则，否则给出具体控制台步骤。Ubuntu 版本、SSH 连接和文件冲突要以现场结果为准。
3. 将子命令换成 `deploy` 并保留原参数。有已指向 VPS 公网 IPv4 的域名才加 `--domain`，无域名则申请受信任的 IP 证书。指定目标用 `--reality-target`，默认在 VPS 端验证候选的 TLS 1.3、HTTP/2 和证书。已有 UFW 会增加应用规则，在干净 VPS 上需要启用 UFW 时可加 `--enable-ufw`。安装保留现有 SSH 认证和端口设计，证书失败时根据日志修复云安全组或 DNS，再运行原命令。
4. 检查输出的 `status.json` 和 `verification.json`。如本机有 Mihomo，用 `--mihomo <可执行文件>` 让部署后自动实连，也可单独运行 `python scripts/verify.py --bundle <交付目录> --mihomo <可执行文件>`。在用户实际使用的网络跑这一检查。服务器端配置通过、公网订阅通过、节点出口和目标网站访问是不同结果。已有系统 TUN 时，独立实例关闭自身 TUN 仍可能受系统路由影响，必要时核对物理网卡和服务器收到的连接来源。
5. 交付本机目录里的 `subscription.txt`、`mihomo.yaml`、`open-panel.cmd`（Windows）或 `open_panel.py`（macOS/Linux）及 `panel-login.txt`。把路径交给用户，凭据留在系统权限保护的目录中。SSH 私钥仍在用户原位置，备份时一并保管。如果用户要求配好本机而且能够操作客户端，就用 Clash/Mihomo 的正常入口导入、选中并验证新节点。否则给出用户可直接导入的文件路径。

服务端采用 3x-ui 原生 Mihomo 导出。面板和内部订阅监听回环地址，Nginx 仅提供订阅和 VMess 的 HTTPS，面板用 SSH 转发。三节点默认无固定限速、流量额度或到期时间。

只有在续期或实际故障需要时读 [references/operations.md](references/operations.md)。两次部署留下的设计取舍、验证边界和故障经验见 [references/field-notes.md](references/field-notes.md)。
