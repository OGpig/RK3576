# 协作说明（并发开发）

最后更新：2026-11-03 14:35 CST

## 当前方案

板端真实屏幕（HDMI/DSI 扫描输出）通过 DRM dma-buf 抓帧，JPEG over WebSocket 推到浏览器；
浏览器鼠标/键盘经 Unix socket 转到 `rkdesk_input.py`，用 uinput 注入 Weston。

| 模块 | 位置 | 说明 |
| --- | --- | --- |
| `RKDesk/` | 本目录 | **主力**：真实屏幕镜像 + 绝对鼠标/键盘，端口 **8088** |
| `RKDesk/ --mode virtual` | 同上 | 兜底：板端自绘桌面 + PTY，不需要 Weston |
| `recovery/` | 本地资料 | 1.1 GB 开发资料 RAR，已 `.gitignore`，不要提交 |

旧 `Visualisation/` 已删除（历史在 Git）；板端 `/tmp/board_agent.py` 重启后不再存在。

## 服务与端口

- `S48rkdesk-input` → `rkdesk_input.py`：创建 `RKDesk AbsMouse` / `RKDesk Keyboard`，
  **必须早于 Weston 启动**（Weston 只在启动时枚举输入设备）。
- `S49weston` → Weston 14.0.1（DRM/GBM），DSI-1 原生 800×1280，`rotate-90` 后逻辑
  1280×800 横向；HDMI 接入后自动 1920×1080。
- `S99rkdesk` → `rkdesk_server.py`，端口 8088，抓屏 + WebSocket。
- `8765`：旧代理已删除；板端 `/tmp` 副本在重启后消失，无需再处理。

**重启 `S99rkdesk` 不会重启 Weston**；只有重启 `S48rkdesk-input` 会重建输入设备并重启
Weston（会关闭板端当前打开的窗口）。

## 关键环境事实（避免重复踩坑）

1. Buildroot 2024.02 / Linux 6.1.115-rt16 / aarch64 / glibc 2.38；
   没有 apt、gcc、git、VNC server。
2. `/usr/bin/weston` 原 ELF 在 10:30 被写坏成 56 字节错误文本。现已恢复为 Python 启动器：
   调用 `/usr/lib/weston/libexec_weston.so.0` 的 `wet_main(argc, argv, NULL)`。
   仓库里的 `RKDesk/deploy/weston` 是启动器源，`deploy.sh` 会自动安装；
   损坏文件备份：`/usr/bin/weston.damaged.20261103`。
   **不要**再把任何命令输出重定向到 `/usr/bin/weston`。
3. `/etc/init.d/S49weston` 的 `stop_weston()` 已改为按
   `ps -eo pid,args | awk '/python3 \/usr\/bin\/weston/'` 杀进程，
   因为 `killall weston` 匹配不到 Python 启动器。
4. `/dev/fb0` 全零，Weston 用 GBM 渲染；`weston-screenshooter` 返回
   `Output capture error: unauthorized`。真实抓屏必须走 DRM dma-buf。
5. 虚拟手写板（tablet）能移动光标，但点击桌面图标不生效；**绝对鼠标**
   （ABS_X/Y + BTN_LEFT，`INPUT_PROP_POINTER`）才会被 Weston 当作 pointer，
   点击/拖动/右键都正常。
6. uinput 新版接口：`UI_DEV_SETUP` 的结构体是 `id` 在前、`name` 在后；
   `UI_ABS_SETUP` 必须带 resolution，否则 libinput 丢弃 tablet。
   `input_event` 在 64 位上是 24 字节（两个 8 字节 timeval），不能用 `llHHi`。
7. 板端 29.1 GB eMMC，可见内存 1.9 GiB，`/userdata/swapfile` 2 GB。

## 已验证

- 浏览器显示真实 Weston 桌面（Chromium/终端/编辑器图标 + 底部面板）；
- 点击底部终端按钮打开 Wayland Terminal，键盘输入
  `uname -a; echo INPUTD-OK` 正常执行；
- 绝对鼠标坐标 1:1，光标包含在抓屏里，右键/滚轮/拖动已接通；
- 拖拽光标时 15.1 fps、约 980 KB/s、平均 64.6 KB/帧；静止 0 KB/s；
- 抓帧 14.7 ms，JPEG q78 约 16 ms，服务 RSS 约 52 MB；
- 重启 `S99rkdesk` 后 Weston PID 不变；
- 重启 `S48rkdesk-input` 后服务端自动重连并重发 `resize 1280×800`，点击 Chromium 仍准确；
- 光标可覆盖四角、Chromium 图标和底部面板，不再跳过区域。

## 已修复的输入问题

- 输入 socket 连接后沿用了 1 秒 connect timeout，空闲 1 秒 reader 会主动断开；
  已在连接成功后切回阻塞模式。
- 守护进程重启后默认几何是 800×1280，服务端重连时必须重发 `resize 1280×800`；
  否则坐标按比例偏移，表现为「跳过 Chromium / 部分开始菜单」。现在由 `InputClient`
  在每次重连时自动同步几何。

## Git 约定（重要：本机不能直接 push）

- 仓库根目录：`F:/data/RK3576`
- `origin` → `https://gh-proxy.com/https://github.com/OGpig/RK3576.git`（**只读镜像，可 fetch**）
- `github` → `https://github.com/OGpig/RK3576.git`（规范地址，本机直连被阻断）
- 本机 `github.com:443` 不可达，`git push` 会失败；推送走 GitHub Git Data API：

```bash
cd F:/data/RK3576
git add -A && git commit -m "feat(rkdesk): ..."

TOKEN=$(printf 'protocol=https\nhost=github.com\n\n' | git credential fill | sed -n 's/^password=//p')
GH_TOKEN="$TOKEN" python RKDesk/tools/push_api.py

# GitHub 会把提交时区归一化为 UTC，SHA 会变；把本地分支对齐到远端
git fetch origin && git reset --soft origin/main
```

- 开工前先 `git fetch origin`；没有未提交修改时可 `git reset --soft origin/main` 对齐。
- 不要 `git push --force`；API 脚本更新 ref 时使用 `force: false`。
- 大文件（`recovery/*.rar`、`node_modules/`、`__pycache__/`）已在 `.gitignore`。

## 待办

- [ ] 接 HDMI 显示器后验证 1920×1080 自动切换与抓屏
- [ ] 板端重新上电后确认 8765 旧代理已消失（`/tmp` 已清）
- [ ] 如需彻底恢复 Weston ELF：从同版本 Buildroot rootfs 取回覆盖
- [ ] 板端中文 IME（当前浏览器输入中文不会进入板端应用）
- [ ] 需要时给 RKDesk 加 token / SSH 隧道说明
