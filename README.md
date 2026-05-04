# Arista 7050QX Web 后台

这是一个本地 Web 后台，面向 Arista DCS-7050QX-32S-F。它可以直接在控制台运行，默认使用本地模拟数据；填写交换机 SSH 或 eAPI 连接后，可刷新真实设备状态并执行只读查询命令。

## 启动

首次运行如缺少依赖：

```powershell
npm install
```

```powershell
node server.js
```

然后打开：

```text
http://localhost:2480
```

如需换端口：

```powershell
$env:PORT=9000; node server.js
```

## 通过 SSH Console 连接真实交换机

1. 确认电脑能 SSH 到交换机：

```powershell
ssh admin@192.168.1.10
```

2. 打开后台的“连接”区域：

- 连接方式选择 `SSH Console`
- 管理地址填写交换机管理 IP
- 端口一般是 `22`
- 填写用户名和密码
- 勾选“启用真实设备”
- 点击“保存连接”

3. 点击“刷新”，或者在命令控制台运行：

```text
show interfaces status
show version
show environment all
```

后台会拦截配置、重启、删除类命令，只允许只读查询命令。

## 直接跑在交换机里面

如果你想访问 `http://交换机IP:2480/`，需要把单文件版本放到 EOS 的 `/mnt/flash` 并在交换机 bash 里启动。

从你的电脑执行：

```powershell
scp .\onbox\arista7050_web.py admin@交换机IP:/mnt/flash/arista7050_web.py
ssh admin@交换机IP
```

进入交换机后执行：

```text
enable
bash
python3 /mnt/flash/arista7050_web.py --host 0.0.0.0 --port 2480
```

然后浏览器打开：

```text
http://交换机IP:2480/
```

如果交换机里没有 `python3` 命令，试：

```text
python /mnt/flash/arista7050_web.py --host 0.0.0.0 --port 2480
```

交换机内置版通过 EOS 本机 CLI 读取状态，不需要在网页里填写 SSH 密码。当前页面会读取 CPU、内存、温度、端口状态、接口实时速率、Kpps、错误计数，并支持点击每个端口查看详情。

## WebUI 功能分支

`codex/webui-operations-dashboard` 分支增加了更完整的交换机 WebUI：

- 自动发现：接口、LLDP 邻居、基础拓扑。
- 告警：环境异常、接口错误计数、有介质但链路未 Up、采集异常。
- 自定义仪表盘基础能力：页面聚合健康、实时转发、实时交换、流量图、事件、告警。
- 表项采集：VLAN、ARP、FDB/MAC address-table。
- 协议采集：OSPF、OSPFv3、BGP summary。
- 集成探测：Syslog、sFlow、NetFlow/IPFIX 相关配置。
- 端口详情：up/down、介质、VLAN、双工、协商速率、RX/TX Mbps、Kpps、错误计数。
- 受控配置：端口启停、接口描述、创建 VLAN、接口加入 VLAN、三层接口、OSPF network、BGP neighbor。

配置动作需要在页面输入 `APPLY`，并通过固定模板生成命令。很多写操作只在有限环境中测试，生产使用前请先用“预览”核对命令，并在维护窗口验证。

如需让局域网内所有 IP 都能访问 `2480`，需要在 EOS control-plane ACL 中放行 TCP/2480，并保存配置。示例：

```text
configure terminal
ip access-list codex-web-2480-cp
   5 permit tcp any any eq 2480
system control-plane
   ip access-group codex-web-2480-cp in
write memory
```

## 通过 eAPI 连接真实交换机

1. 在交换机 EOS 上启用 eAPI，例如：

```text
management api http-commands
   no shutdown
```

2. 打开后台的“连接”区域，连接方式选择 `Arista eAPI`，填写管理 IP、协议、端口、用户名和密码。
3. 勾选“启用真实设备”，保存后点击“刷新”。

## 安全说明

后台只监听本机端口，适合个人电脑或内网运维台使用。连接信息会保存到 `data/config.json`，请不要把带有真实密码的文件提交到公开仓库。命令控制台默认拦截配置、删除、重启等写操作，只允许 `show`、`ping`、`traceroute`、`dir`、`more` 等只读命令。
