# RK3576 开发板真实屏幕远程工作台

把 **TL3576-MiniEVM（RK3576）的 HDMI / DSI 实际输出**镜像到本机浏览器，并用本机鼠标键盘
直接操作板端桌面。画面来自 DRM/KMS 扫描缓冲区，不是自绘 UI；输入通过 uinput 注入
Weston，效果等同于给板子接了一台远程显示器 + 键鼠。

![浏览器中的真实板端桌面](RKDesk/docs/browser-mirror.png)

## 快速开始

```text
http://192.168.31.153:8088
```

浏览器打开即可。点击画面后用本机鼠标/键盘操作板端；底部工具栏支持画质、全屏、粘贴。

- 画面/鼠标键盘：`http://192.168.31.153:8088`
- **双向音频（麦克风）**：`https://192.168.31.153:8443`（自签名证书，首次需手动信任）
- **C 版主方案候选**：`http://192.168.31.153:8089`（画面/输入）+ `https://192.168.31.153:8444`（音频/麦克风）

```bash
# 重新部署（改代码后）
cd RKDesk && bash deploy.sh

# 板端服务
ssh rk3576
/etc/init.d/S48rkdesk-input status   # 虚拟绝对鼠标/键盘
/etc/init.d/S49weston status         # Weston 桌面
/etc/init.d/S99rkdesk status         # 抓屏 + WebSocket
```

## 方案对比

| 方案 | 状态 | 说明 |
| --- | --- | --- |
| **RKDesk mirror** | ✅ 当前主力 | DRM 真实画面 + 绝对鼠标/键盘，端口 **8088** |
| RKDesk virtual | 🧪 兜底 | `--mode virtual`，板端自绘桌面 + PTY，无 Weston 时可用 |
| RKDesk C 8089/8444 | 🚀 主方案候选 | C + DRM + RGA + MPP 硬件 JPEG + Pulse，27.6fps/CPU 9.9%，见 `RKDesk/c-src/` |

旧 `Visualisation/`（Node/noVNC + `board_agent.py` 旧 MVP）已删除，历史仍保留在 Git 中。

## 关键事实

```text
Board      : TL3576-MiniEVM / RK3576-Tronlong
OS         : Buildroot 2024.02
Kernel     : Linux 6.1.115-rt16 (aarch64)
Display    : DSI-1 逻辑 1280x800（原生 800x1280，Weston rotate-90；HDMI 未接，接入自动 1920x1080）
Memory     : 1.9 GiB visible + 2 GiB swapfile
Storage    : 29.1 GB eMMC（rootfs 9.9 GB，4.6 GB 可用）
Python     : 3.11.8, numpy, OpenCV, Pillow, onnxruntime, torch, rknnlite/rknn
SSH        : OpenSSH 9.6p1, root key login
Audio      : PulseAudio 16.1（ES8388 sink monitor + RKDesk 虚拟麦克风）
```

## 文档

- [RKDesk 设计、抓屏/输入原理、性能与限制](RKDesk/README.md)
- [系统说明：架构、链路、决策、性能](RKDesk/docs/ARCHITECTURE.md)
- [实现步骤：分阶段、逐模块、18 个故障复盘](RKDesk/docs/IMPLEMENTATION.md)
- [答辩/面试问答：40 个技术细节](RKDesk/docs/INTERVIEW_QA.md)
- [mlsysbook IDE Setup 实验（RK3576 版）](RKDesk/docs/mlsys-ide-setup.md)
- [板端环境校验报告](RKDesk/docs/env-report.txt)
- [并发开发协作说明](COORDINATION.md)

## 已知问题

- `/usr/bin/weston` 原 ELF 曾被写坏；现已用 `libexec_weston.so` 的 `wet_main()`
  恢复为 Python 启动器，桌面/HDMI 输出正常。损坏文件备份在
  `/usr/bin/weston.damaged.20261103`，详见 `RKDesk/README.md`。
- 旧 `board_agent.py` 不在仓库中；板端副本位于 `/tmp`，重启后自动消失。
  若板子热重连后仍看到 8765 端口，可执行 `kill $(pgrep -f /tmp/board_agent.py)` 停掉。
- **板端没有扬声器/麦克风**：本地播放视频不会从板子出声；请在 HTTPS 远程页面点「声音」。
- 默认无认证，仅限局域网；跨网请用 SSH 隧道。
- 音频走自签名 HTTPS 8443；若浏览器仍禁用麦克风，可 SSH 隧道访问 `http://localhost:8088`。
