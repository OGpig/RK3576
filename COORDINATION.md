# 协作说明（并发开发）

最后更新：2026-11-03 14:05 CST

## 当前分工

| 模块 | 位置 | 说明 |
| --- | --- | --- |
| `RKDesk/` | 本目录 | 板端渲染虚拟桌面 + WebSocket 推流 + PTY，端口 **8088** |
| `Visualisation/` | 旧 MVP | Node/Express + noVNC 外壳 + `board_agent.py`，端口 **8765** |
| `recovery/` | 本地资料 | 1.1 GB 开发资料 RAR，已加入 `.gitignore`，不要提交 |

## 端口与服务

- `8088`：RKDesk（当前主力），`/etc/init.d/S99rkdesk`
- `8765`：旧 `board_agent.py`（读 `/dev/fb0`，画面全黑，已无用途）
- `5900`：未使用（板端没有 VNC 服务）

如果确认旧代理不再需要，可执行：

```bash
kill $(pgrep -f /tmp/board_agent.py)
```

**不要**在未沟通的情况下重启或改写 RKDesk 的 `/opt/rkdesk` 目录。

## 关键环境事实（避免重复踩坑）

1. 板端是 **Buildroot 2024.02 / Linux 6.1.115-rt16 / aarch64 / glibc 2.38**，
   没有 apt、没有 gcc、没有 git、没有 VNC server。
2. `/usr/bin/weston` **已损坏**：现在只有 56 字节，内容是
   `/etc/init.d/S49weston: line 20: 11: Bad file descriptor`。
   这不是 RKDesk 造成的；恢复需要同版本 rootfs 或重刷，详见 `RKDesk/README.md`。
   **不要**再把任何命令输出重定向到 `/usr/bin/weston`。
3. `/dev/fb0` 全零，原因是 Weston 没运行；它也不是 Weston 的合成输出，
   所以「读 fb0 当屏幕」这条路走不通。
4. 板端已有 Python 3.11.8 + Pillow 13 + numpy 1.26.4 + OpenCV 5.0.0 +
   onnxruntime 1.31 + torch 2.2 + rknnlite/rknn。
5. 板端实际是 **29.1 GB eMMC**（用户记的 16/32 GB，实测 32 GB 档），
   可见内存 1.9 GiB，`/userdata/swapfile` 有 2 GB swap。
6. Weston 的 DRM 输出是 `DSI-1` 800×1280；HDMI/DP 均 disconnected。
   RKDesk 不依赖它，默认虚拟桌面 1280×800。

## 已验证

- `http://192.168.31.153:8088/` 浏览器可出画面；
- 鼠标点击聚焦、拖动窗口、滚轮回看；
- 键盘输入执行 `echo hello-from-browser; uname -a; pwd` 成功；
- 中文输入 `echo "中文输入测试 OK"` 正常渲染；
- 拖动窗口时 12 fps、约 600 KB/s；静止时 1 fps 心跳、约 50 KB/s；
- 服务 RSS ≈ 41 MB，CPU 平均 ≈ 12%。

## Git 约定

- 仓库根目录：`F:/data/RK3576`
- 远端：`https://github.com/OGpig/RK3576`
- 提交前先 `git pull --rebase`，避免覆盖对方提交；
- 大文件（`recovery/*.rar`、`node_modules/`、`__pycache__/`）已在 `.gitignore`；
- 提交信息用 `feat(rkdesk): ...` / `docs: ...` / `fix: ...`。

## 待办

- [ ] 本机安装 VS Code + Remote-SSH，完成首次远程连接（见 `RKDesk/docs/mlsys-ide-setup.md`）
- [ ] 决定是否停止 8765 旧代理，释放约 14% CPU
- [ ] 如需恢复 Weston：从同版本 Buildroot rootfs 取回 `/usr/bin/weston`
- [ ] 外接 USB 摄像头后补 `v4l2-ctl --list-devices` 验证
- [ ] 若要长期使用，给 RKDesk 加 systemd/init 自启（已加 S99rkdesk）与 token
