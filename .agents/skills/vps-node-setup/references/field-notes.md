# 两次部署提炼出的做法

## 第一次

围绕 Ubuntu、3x-ui 和三协议搭建，借鉴了参考项目中 Nginx 在前、VMess WebSocket 只监听本机的结构。为 REALITY 保留自己的 TCP 监听，VMess 用另一个 HTTPS 端口，Hy2 使用 UDP。交付真实的 Mihomo YAML，面板通过 SSH 通道访问，并保留一键启动器。后来本机资料误删，服务器面板数据库仍在；通过服务商控制台添加新公钥，验证新 SSH 登录后从服务端恢复了面板入口和订阅。

## 第二次

公网 SSH 可用时，应用端口仍可能被云安全组拦截。检查云规则并补齐 TCP 80、REALITY、HTTPS 和 Hy2 UDP 后，才完成 IP 证书签发和三个节点的外网连通。使用官方发行包和 SHA-256，确认 3x-ui 原生导出 REALITY 的 ML-KEM 兼容参数以及 Hy2 的 Salamander 字段。重启后验证了服务自启；模拟续期和独立 Mihomo 的三节点出口及网站访问均有实测记录。

曾用同网络运营方的公开 HTTPS 镜像做 REALITY 目标，并在 VPS 端核对 TLS 1.3、HTTP/2 和证书。这个选择随 VPS 而变，安装器在当前服务器重新测试候选。后来遇到 Google 路径短时异常，Cloudflare 和出口 IP 仍能通过，说明单个测速地址不足以判断三协议是否可用。

## 通用默认值

应用端口与历史部署一致，SSH 沿用所选 VPS 的真实端口。首次执行检查端口、提前确认公网入口、生成新的凭据。一般部署无需重装系统、切换防火墙默认策略或改 SSH 认证。是否升级系统、重启和停用 SSH 密码，由本次任务范围决定。

第一版安装器将两次分批运行的步骤合并成一个 SSH 命令，保留了证书校验、回环面板和独立客户端验证。验收结果只覆盖实际跑过的机器、网络和时间。端口、线路和目标站的选择都无法保证长期可达。

参考结构与协议：

- [vps_deployment_ai_tools](https://github.com/carrotgithub/vps_deployment_ai_tools)
- [3x-ui v3.11.0](https://github.com/MHSanaei/3x-ui/releases/tag/v3.11.0)
- [Xray v26.11.9](https://github.com/XTLS/Xray-core/releases/tag/v26.11.9)
- [REALITY 配置](https://xtls.github.io/config/transports/reality.html)
- [Let’s Encrypt IP 证书与 Certbot](https://letsencrypt.org/2026/03/11/shorter-certs-certbot/)
