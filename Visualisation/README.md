# RK3576 Remote Console

这是一个主机端浏览器控制台的 MVP 外壳：目标是通过局域网查看并操作 RK3576 板端图形界面。

## 当前方案

- 主机端：Node.js + Express，默认只监听 `127.0.0.1:5173`
- 浏览器端：控制台 UI，预留画面流、鼠标、键盘、全屏和缩放操作
- 板端实际系统：Buildroot 2024.02 + Linux 6.1.115-rt16 + aarch64
- 板端图形栈：Weston 14.0.1 / Wayland DRM，不是 X11
- 板端当前显示输出：`DSI-1`，分辨率约 `800x1280`；HDMI 未连接
- 原始 MVP 协议：VNC（默认目标 `192.168.31.153:5900`）
- 推荐协议：Weston/Wayland 画面流 + `/dev/uinput` 输入注入，避免在板端安装重型 VNC 桌面服务

## 启动

主机端：

```bash
cd F:/data/RK3576/Visualisation
npm install
npm start
```

浏览器打开：<http://localhost:5173>

Windows 也可以双击 `launch_console.bat`。

板端代理（当前使用 root SSH key，可直接复制并启动）：

```bash
scp board_agent.py root@192.168.31.153:/tmp/board_agent.py
ssh root@192.168.31.153 'python3 /tmp/board_agent.py --port 8765 --quality 65'
```

然后回到浏览器点击“连接板端桌面”。控制台会优先访问：

```text
http://192.168.31.153:8765/healthz
http://192.168.31.153:8765/frame.jpg
```

如果板端代理没有启动，前端仍保留 VNC 备用路径。

可用环境变量：

```bash
BOARD_IP=192.168.31.153 \
TRANSPORT="Weston/Wayland stream" \
STREAM_TARGET=192.168.31.153:8765 \
PORT=5173 npm start
```

## 板端诊断结果

已经使用当前主机上的 SSH 密钥成功登录：

```text
ssh root@192.168.31.153
hostname: RK3576-Tronlong
uid=0(root)
```

板端信息：

- 系统：Buildroot 2024.02
- 内核：Linux 6.1.115-rt16
- 架构：`aarch64`
- Weston：14.0.1，已经在运行
- 图形输出：`DSI-1`，约 `800x1280`
- GPU：Mali-G52，OpenGL ES 3.2
- 根文件系统：约 9.9G，总体已使用约 4.9G
- 内存：系统实际识别约 1.9GiB，可用约 1.7GiB；这说明板卡可能是 2GB 配置，或当前 BSP/设备树没有识别到全部内存
- Swap：未配置
- 当前未安装：`vncserver`、`x11vnc`、`wayvnc`、`scrcpy`
- 当前没有 TCP 5900 VNC 服务
- `/dev/uinput` 存在，适合后续做远程鼠标/键盘注入
- GStreamer 已包含 JPEG 编码、Rockchip MPP JPEG 编码、H.264/H.265 RTP 能力，可用于低资源画面流

## 推荐实施路线

不要先在板端安装完整桌面或 VNC。当前板端已经运行 Weston，推荐编写一个小型 root 权限代理：

1. 通过 Weston 的 `screen-share.so` 或 DRM/Wayland 捕获当前 `DSI-1` 画面；
2. 使用板端已有 GStreamer / Rockchip MPP 编码为 JPEG 或 H.264；
3. 通过一个 TCP/WebSocket 服务发送到主机浏览器；
4. 主机将浏览器的鼠标/键盘事件发送回板端；
5. 代理通过 `/dev/uinput` 注入鼠标和键盘事件；
6. 画面分辨率默认按 `800x1280` 处理，必要时在板端降到 `400x640` 以降低带宽和内存开销。

这条路径比 VNC 更适合当前 Buildroot 镜像，也更适合 4GB/16GB 级别的板端资源。

## 当前已核实的网络状态

- `192.168.31.153` ICMP 可达；
- TCP 22 开放，服务标识为 `OpenSSH_9.6`；
- root 可使用现有 SSH 密钥无密码登录；
- 5900、5555、8000、8080 当前未探测到可用服务。

## 安全提醒

当前板端代理是局域网实验版：默认监听 `0.0.0.0:8765`，没有账号密码或 TLS。不要把端口映射到公网；如果要跨公网使用，需要先加 SSH 隧道、反向代理认证或 VPN。代理需要 root 权限才能读取 `/dev/fb0` 和写入 `/dev/uinput`。

## 板端探测

若后续更换镜像或板卡，可用以下脚本重新读取环境：

```bash
bash board_probe.sh 192.168.31.153 root
```

脚本只读取系统信息，不安装、不修改板端文件。
