# RKDesk C mirror（性能对比版）

板端 C 实现，和 Python 版（8088/8443）并行运行，用于对比画面镜像的吞吐/资源占用。
C 版现在包含 **视频、输入、PulseAudio 双工音频和 HTTPS**。

## 它做什么

- 直接 `ioctl` DRM/KMS 取当前 CRTC framebuffer，`dma-buf` mmap，不经过 libdrm；
- 原生扫描缓冲直接交给 **libjpeg-turbo** 编码（板端已有 `libturbojpeg.so.0`）；
- 8089 提供页面、`/ws` JPEG 帧、`/healthz`、`/api/screenshot.jpg`；
- 8444 提供 HTTPS/WSS，给浏览器 `getUserMedia` 提供 secure context；
- `/av` 提供 PCM 双工音频：
  - 板端→浏览器：`parec` 抓默认 sink 的 monitor，48k/2ch；
  - 浏览器→板端：`pacat` 写入 `rkdesk_c_mic` null sink，remap 成
    `rkdesk_c_mic_source`（RKDesk_C_Microphone）；
- 输入事件直接转发到现有 `rkdesk_input.py` 的 Unix socket，复用绝对鼠标/键盘；
- DSI 800×1280 原始帧不在 C 里旋转，浏览器 canvas 用 GPU 旋转成 1280×800。

C 版虚拟麦克风使用独立名字，避免和 Python 版的 `rkdesk_mic` 互相清理。

## 编译

依赖：

- Zig 0.13 Windows（`ZIG` 环境变量指向 `zig.exe`）；
- 板端运行库拷贝到主机 `LIBDIR`：
  - `libturbojpeg.so.0`
  - `libssl.so.3`
  - `libcrypto.so.3`

```bash
scp root@192.168.31.153:/usr/lib/libturbojpeg.so.0 \
    root@192.168.31.153:/usr/lib/libssl.so.3 \
    root@192.168.31.153:/usr/lib/libcrypto.so.3 \
    F:/data/toolchains/board-libs/
cd RKDesk/c-src
ZIG=F:/data/toolchains/zig/zig-windows-x86_64-0.13.0/zig.exe \
LIBDIR=F:/data/toolchains/board-libs \
bash build.sh
```

产物 `c-src/rkdesk_c` 是 aarch64 glibc ELF，动态链接板端
`libturbojpeg.so.0`、`libssl.so.3`、`libcrypto.so.3`。

## 部署/运行

`deploy.sh` 会顺带把已编译的 `c-src/rkdesk_c` 复制到 `/opt/rkdesk/rkdesk_c`，
但不会自动启动。

```bash
ssh root@192.168.31.153 '
  pkill -x rkdesk_c 2>/dev/null || true
  nohup /opt/rkdesk/rkdesk_c \
      --port 8089 --https-port 8444 \
      --fps 30 --quality 78 --subsamp 422 \
      --cert /opt/rkdesk/certs/rkdesk.crt \
      --key  /opt/rkdesk/certs/rkdesk.key \
      >/var/log/rkdesk-c.log 2>&1 &
'
```

- 画面/输入：**http://192.168.31.153:8089/**
- 音频/麦克风：**https://192.168.31.153:8444/**（自签名证书）
- 8089 页面也能听板端声音；点麦克风会自动跳到 8444。
- 停止：`ssh root@192.168.31.153 'pkill -x rkdesk_c'`。

参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--port` | 8089 | HTTP/WebSocket 端口 |
| `--https-port` | 8444 | HTTPS/WSS 端口（0 关闭） |
| `--cert` / `--key` | `/opt/rkdesk/certs/rkdesk.crt|key` | TLS 证书 |
| `--fps` | 30 | 目标帧率 |
| `--quality` | 80 | JPEG 质量 |
| `--subsamp` | 420 | `420` 更快/更小，`422` 文字更清晰 |
| `--device` | `/dev/dri/card0` | DRM 设备 |

## 音频验证

- 板端→C `/av`：约 2.4 s 收到 441 KB，峰值 2810（测试音）；
- 浏览器 PCM→`rkdesk_c_mic_source`：2 s 收到 201 KB，峰值 10962；
- 默认 source 在停止/断开后恢复为板端物理麦克风；
- 无头 Edge HTTPS 页面：`isSecureContext=true`，假麦克风可推流。

## 实测对比（同一块 DSI 800×1280，q78，4:2:2，6 秒拖动）

| 指标 | Python 8088 | C 8089 |
| --- | --- | --- |
| 帧率 | 15.0 fps | **29.8 fps** |
| 带宽 | 807 kbps | 1660 kbps |
| 平均帧 | 53.8 KB | 55.7 KB |
| 服务端 encode | 15.9 ms | 20.7 ms |
| 进程 CPU | 64.8% | 64.3% |
| RSS | 52 MB | **7.8 MB** |

结论：C 版在相近 CPU 下把帧率翻倍，内存约为 Python 的 1/7；
音频/TLS 也已补齐，但 C 版仍不处理 HDMI 热插拔/CRTC 切换。

C 的 `encode_ms` 看起来略高，是因为 Python 的数值只统计 JPEG，而 C 直接从 DRM map 编码；
Python 还在帧循环里做 PIL 旋转/拷贝。整体吞吐以 fps/CPU 为准。

## 限制

- C 版固定启动时的 CRTC，不跟随 HDMI 热插拔；
- 音频为 PCM，无 Opus/WebRTC；
- 无摄像头采集；
- 自签名证书首次需手动信任；
- 默认无认证，仅可信局域网。
