# RKDesk C mirror（性能对比版）

板端 C 实现，和 Python 版（8088）并行运行，用于对比画面镜像的吞吐/资源占用。

## 它做什么

- 直接 `ioctl` DRM/KMS 取当前 CRTC framebuffer，`dma-buf` mmap，不经过 libdrm；
- 原生扫描缓冲直接交给 **libjpeg-turbo** 编码（板端已有 `libturbojpeg.so.0`）；
- 8089 端口提供独立页面、`/ws` JPEG 帧、`/healthz`、`/api/screenshot.jpg`；
- 输入事件直接转发到现有 `rkdesk_input.py` 的 Unix socket，复用绝对鼠标/键盘；
- DSI 800×1280 原始帧不在 C 里旋转，浏览器 canvas 用 GPU 旋转成 1280×800。

**不含音频/HTTPS**；需要听板端声音、用浏览器麦克风请继续使用 Python 版
`https://192.168.31.153:8443`。C 版是画面/输入的独立性能通道。

## 编译

依赖：
- Zig 0.13 Windows（`ZIG` 环境变量指向 `zig.exe`）；
- 板端 `libturbojpeg.so.0` 拷到主机（`LIBDIR`）。

```bash
cp /path/to/zig.exe ...                 # 或设置 ZIG
scp root@192.168.31.153:/usr/lib/libturbojpeg.so.0 F:/data/toolchains/board-libs/
cd RKDesk/c-src
ZIG=F:/data/toolchains/zig/zig-windows-x86_64-0.13.0/zig.exe \
LIBDIR=F:/data/toolchains/board-libs \
bash build.sh
```

产物 `c-src/rkdesk_c` 是 aarch64 glibc ELF，动态链接板端 `libturbojpeg.so.0`。

## 部署/运行

`deploy.sh` 会顺带把已编译的 `c-src/rkdesk_c` 复制到 `/opt/rkdesk/rkdesk_c`，但不会自动启动。

```bash
ssh root@192.168.31.153 '
  pkill -x rkdesk_c 2>/dev/null || true
  nohup /opt/rkdesk/rkdesk_c --port 8089 --fps 30 --quality 78 --subsamp 422 \
      >/var/log/rkdesk-c.log 2>&1 &
'
```

打开 **http://192.168.31.153:8089/**。
停止：`ssh root@192.168.31.153 'pkill -x rkdesk_c'`。

参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--port` | 8089 | HTTP/WebSocket 端口 |
| `--fps` | 30 | 目标帧率 |
| `--quality` | 80 | JPEG 质量 |
| `--subsamp` | 420 | `420` 更快/更小，`422` 文字更清晰 |
| `--device` | `/dev/dri/card0` | DRM 设备 |

## 实测对比（同一块 DSI 800×1280，q78，4:2:2，6 秒拖动）

| 指标 | Python 8088 | C 8089 |
| --- | --- | --- |
| 帧率 | 15.0 fps | **29.8 fps** |
| 带宽 | 807 kbps | 1660 kbps |
| 平均帧 | 53.8 KB | 55.7 KB |
| 服务端 encode | 15.9 ms | 20.7 ms |
| 进程 CPU | 64.8% | 64.3% |
| RSS | 52 MB | **1.7 MB** |

结论：C 版在相近 CPU 下把帧率翻倍，内存约为 Python 的 1/30；代价是当前只做画面/输入，
没有音频、HTTPS 和虚拟麦克风，也不处理 HDMI 热插拔/CRTC 切换。

C 的 `encode_ms` 看起来略高，是因为 Python 的数值只统计 JPEG，而 C 直接从 DRM map 编码；
Python 还在帧循环里做 PIL 旋转/拷贝。整体吞吐以 fps/CPU 为准。
