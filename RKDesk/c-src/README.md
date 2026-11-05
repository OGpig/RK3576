# RKDesk C mirror（硬件加速版）

板端 C 实现，和 Python 版（8088/8443）并行运行。它现在是 **视频/输入/音频/HTTPS 完整**的
C 主方案候选：DRM 直读、RGA 转 NV12、Rockchip MPP 硬件 JPEG、PulseAudio 双工、OpenSSL TLS。

## 数据通路

```text
DRM CRTC fb_id → dma-buf mmap (BGRX, pitch)
  → RGA BGRX→NV12（硬件）
  → mppjpegenc（VPU 硬件 JPEG）
  → WebSocket /ws → 浏览器 Canvas 旋转 90°
  → 输入事件 → rkdesk_input.py → uinput → Weston
  → /av PCM 双工 → PulseAudio monitor / rkdesk_c_mic_source
```

- **RGA**：`imcvtcolor_t` 把 800×1280 BGRX 转 NV12，实测约 5ms，CPU 占用低；
- **MPP**：`appsrc → mppjpegenc → appsink`，走 `/dev/mpp_service`，q85 单帧约 7ms；
- **TurboJPEG 兜底**：`--encoder turbo` 或 RGA/MPP 初始化失败时使用；
- **静态优化**：无输入时 256B 采样哈希 + 目标帧间隔休眠；有输入事件（move/key/button）
  直接 `g_force_frame`，跳过哈希；
- **每客户端最新帧邮箱**：采集线程只写 pending，发送线程独立 SSL_write；慢客户端丢旧帧，
  不阻塞采集；
- **TCP_NODELAY + 512KB SO_SNDBUF**：降低 Nagle 延迟；
- **CRTC 每帧刷新**：Weston page flip 会换 fb_id，必须重查 CRTC/GETFB/PRIME，否则会一直编码旧帧；
- DSI 原生 800×1280 不在 C 里旋转，浏览器 Canvas GPU 旋转成 1280×800。

## 音频 / HTTPS

- `/av` 下行：`parec` 抓默认 sink 的 monitor，48k/2ch/20ms；
- `/av` 上行：`pacat` 写 `rkdesk_c_mic` null sink，remap 成
  `rkdesk_c_mic_source`（RKDesk_C_Microphone）；
- 默认 source 保存 `/var/run/rkdesk-c-mic.previous`，停止/断开/重启恢复；
- 8444 HTTPS：手写 OpenSSL ABI，链接板端 `libssl.so.3`/`libcrypto.so.3`；
- 页面音频控件：声音/源/麦克风/测试音；HTTP 8089 点麦克风自动跳 8444。

## 编译

依赖（全部从板端复制到 `LIBDIR`）：

```text
libturbojpeg.so.0
libssl.so.3
libcrypto.so.3
librga.so.2
libgstreamer-1.0.so.0
libgstapp-1.0.so.0
libgobject-2.0.so.0
libglib-2.0.so.0
```

以及主机 Zig 0.13 Windows。RGA 头文件已放在 `c-src/rga_include/`（Apache-2.0）。

```bash
cd RKDesk/c-src
ZIG=F:/data/toolchains/zig/zig-windows-x86_64-0.13.0/zig.exe \
LIBDIR=F:/data/toolchains/board-libs \
bash build.sh
```

产物 `c-src/rkdesk_c` 是 aarch64 glibc ELF（约 136KB）。

## 部署/运行

`deploy.sh` 会复制二进制到 `/opt/rkdesk/rkdesk_c`，但不会自动启动。

```bash
ssh root@192.168.31.153 '
  ps -eo pid,comm | awk "\$2==\"rkdesk_c\"{print \$1}" | xargs -r kill
  nohup /opt/rkdesk/rkdesk_c \
      --port 8089 --https-port 8444 \
      --fps 30 --quality 85 \
      --cert /opt/rkdesk/certs/rkdesk.crt \
      --key  /opt/rkdesk/certs/rkdesk.key \
      >/var/log/rkdesk-c.log 2>&1 &
'
```

- 画面/输入：**http://192.168.31.153:8089/**
- 音频/麦克风：**https://192.168.31.153:8444/**
- 8089 页面也能听声音；点麦克风自动跳 8444。

参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--encoder` | `mpp` | `mpp`（RGA+MPP）或 `turbo`（TurboJPEG） |
| `--port` / `--https-port` | 8089 / 8444 | HTTP / HTTPS |
| `--fps` | 30 | 目标帧率 |
| `--quality` | 80 | 1–99；MPP 4:2:0 建议 85–90 |
| `--subsamp` | 420 | 仅 TurboJPEG：420/422 |
| `--cert` / `--key` | `/opt/rkdesk/certs/...` | TLS 证书 |
| `--device` | `/dev/dri/card0` | DRM 设备 |

## 实测对比（DSI 800×1280，6 秒指针拖动）

| 指标 | Python 8088 | C Turbo | **C MPP+RGA** |
| --- | --- | --- | --- |
| 帧率 | 15.0 fps | 26.8 fps | **27.6 fps** |
| 带宽 | 807 kbps | 1282 kbps | **1234 kbps** |
| 平均帧 | 53.8 KB | 47.8 KB | 44.7 KB |
| 服务端 encode | 15.9 ms | 19.6 ms | **7.4 ms** |
| 进程 CPU | 64.8% | 35.8% | **9.9%** |
| RSS | 52 MB | 9.4 MB | 18.1 MB |

- Python：q78/4:2:2；C Turbo：q78/4:2:2；C MPP：q85/4:2:0。
- C MPP 帧率略低于 Turbo，但 CPU 约为 1/3.6、encode 约 1/2.6，带宽更低；
- C 静止（客户端连接但画面不变化）：**7.2% CPU**，只发 1 帧；
- C 无客户端：约 0–1% CPU（MPP/GStreamer 基础线程）；
- MPP 4:2:0 对文字略软，建议 `--quality 90`；Turbo 更锐但 CPU 高；
- GStreamer/OpenSSL 使 RSS 从 9.4MB 升到 18.1MB，仍约为 Python 的 1/3。

## 结论

- **C 已具备作为主方案的能力**：视频/输入/音频/HTTPS、硬件 JPEG、静态省流、慢客户端隔离；
- 适合：日常镜像、视频/动画、低 CPU 场景；
- Python 仍保留作 fallback：HDMI 热插拔/CRTC 切换、`--mode virtual`、更完整的板端面板；
- 若要正式切换为默认入口，需把 C 的端口改为 8088/8443，并补 HDMI 热插拔与 UI 细节。

## 限制

- C 固定启动时 CRTC，不跟随 HDMI 热插拔；
- MPP 管道为 4:2:0，文字不如 Turbo 锐；
- 无摄像头采集；
- 自签名证书首次需手动信任；
- 默认无认证，仅可信局域网。
