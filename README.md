# VPS Node Setup Skill

把一台新的 Ubuntu 24.04 LTS 或 Debian 13 VPS 配成由 3x-ui 管理的个人代理服务器，生成 Clash/Mihomo 订阅和一键打开面板的入口。基于两次实际部署整理，包含初次安装、断点重跑、证书续期和连通验证。

## 交给 AI 部署

在 Codex 中打开本仓库，或请支持 Agent Skills 的客户端安装仓库中的 `.agents/skills/vps-node-setup`。也可以复制这句话，让能运行本地命令和 SSH 的 AI 先克隆仓库再调用 Skill：

> 请从 https://github.com/ChristosTJC/vps-node-setup-skill 安装并调用 $vps-node-setup。帮我在 VPS 上配置 REALITY、VMess 和 Hy2，用 3x-ui 管理并生成 Mihomo 订阅。先检查系统和现有 SSH 连接。

准备 VPS 的公网 IPv4、现有 SSH 端口、登录账号和本机密钥。没有域名也能用。只有初始密码时，让 OpenSSH 在本机终端提示输入，或通过服务商控制台添加公钥。

## 直接运行

适用环境为 **Ubuntu 24.04 LTS 或 Debian 13**，均要求 x86_64（amd64）、systemd、root 或免交互 sudo。脚本会通过 SSH 自动识别发行版。运行脚本的电脑需要 Python 3.10+ 和 OpenSSH。先对照服务商控制台核对主机指纹，再将其保存到 `known_hosts`。

```sh
git clone https://github.com/ChristosTJC/vps-node-setup-skill.git
cd vps-node-setup-skill
python .agents/skills/vps-node-setup/scripts/deploy.py preflight --host VPS_IP --port SSH_PORT --identity KEY_PATH
python .agents/skills/vps-node-setup/scripts/deploy.py deploy --host VPS_IP --port SSH_PORT --identity KEY_PATH
```

替换 `VPS_IP`、`SSH_PORT`、`KEY_PATH`。macOS/Linux 通常用 `python3`。使用其他账号时加 `--user`。有直连域名可加 `--domain`。实际任务如果明确要求自定义端口，可使用 `--reality-port`、`--https-port` 和 `--hy2-port`。

在服务商安全组保留原 SSH 规则，再按下表添加入站。脚本会同步已启用的 UFW；如需在干净 VPS 上启用它，可加 `--enable-ufw`。

| 入站 | 用途 |
| --- | --- |
| TCP 80 | Let’s Encrypt 校验。Certbot 在签发或续期时监听 |
| TCP 443 | VLESS + REALITY + Vision |
| TCP 33417 | VMess WebSocket + TLS、Mihomo HTTPS 订阅 |
| UDP 46289 | Hysteria 2 + TLS + Salamander |

如果端口被占用、云安全组尚未放行或域名解析不符，按提示处理后用同一条命令重跑。服务端脚本固定经核对的 3x-ui v3.11.0 和 Xray v26.11.9 发行包，校验 SHA-256 后安装。

## 导入和管理

交付目录默认是 `~/.vps-node-setup/<公网IPv4>/`，可用 `--bundle` 指定仓库以外的目录。

- 将 `subscription.txt` 中的地址添加到支持 **Mihomo** 的客户端，或直接导入 `mihomo.yaml`。选中该配置和要使用的节点。
- Windows 双击 `open-panel.cmd`。macOS/Linux 在交付目录运行 `python3 open_panel.py`。面板登录信息在 `panel-login.txt`，面板只能通过 SSH 通道访问。
- 部署时加 `--mihomo <可执行文件>` 可从当前电脑实测三个节点。如果本机已有命令行 Mihomo，可直接用它。已生成资料后可单独重测：

  ```sh
  python .agents/skills/vps-node-setup/scripts/verify.py --bundle BUNDLE_DIR --mihomo MIHOMO_PATH
  ```

部署记录 `status.json` 和实测记录 `verification.json` 会区分服务状态、HTTPS 订阅、三个出口和目标网站结果。实际通达取决于验证时的客户端网络。默认未配置节点限速、流量额度或到期时间，VPS 套餐的限制仍然适用。

请私下保管交付目录和原 SSH 密钥。证书续期、常见故障和恢复方法见 [操作与排查](.agents/skills/vps-node-setup/references/operations.md)，方案来源和两次部署的经验见 [部署笔记](.agents/skills/vps-node-setup/references/field-notes.md)。
