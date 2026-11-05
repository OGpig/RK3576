# RKDesk 答辩 / 技术面试问答

> 配套：[系统说明](ARCHITECTURE.md) · [实现步骤](IMPLEMENTATION.md)  
> 用法：先看“30 秒版本”，再按分组准备；每题都给出“追问方向”，避免只背结论。

---

## 30 秒版本

RKDesk 解决的是“Buildroot 精简系统没有 VNC，如何远程看真实屏幕并操作”的问题。
视频从 DRM/KMS 的 CRTC framebuffer 经 dma-buf 读取，JPEG over WebSocket 到浏览器；
输入用 uinput 绝对鼠标/键盘注入 Weston；音频用 PulseAudio monitor 下行、null sink +
remap source 做虚拟麦克风上行。Python 版功能完整，C 版做性能对比：
C MPP 27.6fps/CPU 9.9%，Python 15fps/64.8%；RSS 18MB vs 52MB。

---

## 一、架构与选型

### Q1：为什么不用 VNC？

板端是 Buildroot，没有 `vncserver`/`x11vnc`/`wayvnc`，也没有包管理器；
Weston 只编译了 DRM 后端，没有 VNC/headless 后端；`weston-screenshooter` 对普通客户端
返回 `Output capture error: unauthorized`。读 `/dev/fb0` 又是全黑，因为 Weston 用 GBM
渲染，fbdev 只是模拟层。最后只能走 DRM/KMS 真实 scanout。

**追问**：VNC 和远程桌面的本质区别？
VNC 是“像素帧缓冲 + 输入事件”的远程协议；我们实现的是同一类模型，只是传输层换成
WebSocket、抓帧换成 KMS，没有实现 RFB 协议。

### Q2：为什么读 CRTC framebuffer，而不是 `/dev/fb0`？

`/dev/fb0` 是 fbdev 兼容层，不跟踪 GBM/DRM 的 scanout；Weston 把画面渲染到 GBM buffer，
再挂到 KMS plane 上扫描。CRTC 的 `fb_id` 才是当前真正输出的 buffer。

**追问**：怎么确认？
抓一帧看是否包含真实桌面；再对比 `/dev/fb0` 全黑。

### Q3：解释 CRTC、framebuffer、plane、dma-buf。

- CRTC：扫描输出控制器，决定 timing 和当前 framebuffer；
- framebuffer：一块带格式和 pitch 的像素内存；
- plane：图层（primary/cursor/overlay），挂到 CRTC；
- dma-buf/PRIME：把 GPU/显示 buffer 以 fd 导出，供其他进程 mmap。

**追问**：零拷贝吗？
从 GPU 到用户态是 dma-buf mmap，不复制整帧；但 JPEG 编码仍然要读像素并编码，
端到端不是严格零拷贝。

### Q4：为什么用 WebSocket + JPEG，不用 WebRTC？

WebRTC 需要 ICE/DTLS/SDP/编码器，板端依赖重；局域网下 WebSocket + JPEG 足够，
还能在同一条连接上做输入 JSON。代价是带宽高于 H.264，且没有抗丢包。

**追问**：什么时候会换 WebRTC？
需要公网、弱网、低延迟音频/视频会议级体验时；或引入 Opus/H.264 硬件编码后。

### Q5：为什么 Python 和 C 两套实现？

Python 版优先功能完整：视频、输入、音频、HTTPS、热插拔恢复；C 版用来回答“去掉
解释器/对象/旋转后吞吐上限是多少”，并验证性能瓶颈。两者共享同一个输入守护进程，
可以同时运行对比。

**追问**：C 版音频怎么做的？
C 版也实现 `/av`：`parec` 抓默认 sink monitor，`pacat` 写入 `rkdesk_c_mic` 并
remap 成 `rkdesk_c_mic_source`；HTTPS 8444 用板端 libssl/libcrypto 提供安全的
`getUserMedia` 上下文。虚拟麦克风名与 Python 版分开，避免互相清理。

---

## 二、视频、DRM 与编码

### Q6：一帧从板端到浏览器的完整路径？

```text
Weston/GPU → GBM buffer → KMS CRTC → fb_id
→ PRIME_HANDLE_TO_FD → mmap BGRX
→ rotate/JPEG（Python 或 C）
→ WebSocket binary → createImageBitmap → canvas
```

**追问**：哪一步最慢？
Python：抓帧 ~14.7ms + JPEG ~15.9ms；C：TurboJPEG ~20ms（口径），但省掉 Python
对象/拷贝，整体 fps 更高。

### Q7：BGRX 和 pitch 是什么？为什么要 pitch？

BGRX 是每像素 B,G,R,X 四字节，X 忽略；pitch 是一行占用的字节数，可能因对齐大于
`width*4`。按 `width*4` 读会斜切；Pillow 的 `frombuffer(..., "BGRX", pitch, 1)` 和
TurboJPEG 的 `pitch` 参数都是为此。

### Q8：DSI 原生 800×1280，怎么变成 1280×800？

Weston 配 `transform=rotate-90`，逻辑输出变成 1280×800；原始 buffer 仍是 800×1280。
Python 抓帧后旋转 -90 再编码；C 直接编码原生帧，浏览器 Canvas 旋转。
输入要做逆变换。

**追问**：为什么不只在浏览器 CSS 旋转？
那样 Weston 逻辑桌面仍是竖屏，图标/面板布局不对；必须让合成器按 1280×800 布局。

### Q9：90° 旋转的坐标公式怎么推？

设备归一化 `u=dx/1280, v=dy/800`，Weston 按原生轴 rotate-90：

```text
lx = (1-v)*1280
ly = u*800
```

反解：

```text
dx = 1.6*ly
dy = 800 - 0.625*lx
```

**追问**：如果 transform 变成 270 呢？
同理重推：`dx=(1-ly/H)*W, dy=(lx/W)*H`；不要死记公式，按“归一化 + 轴交换/翻转”推。

### Q10：为什么 C 版不在 C 里旋转？

第一版逐像素旋转，`encode_ms=44ms`，只有 23fps。旋转是跨步访问：读源列、写目标行，
1M 像素缓存不友好。改成 C 编码原生帧，浏览器 Canvas 用 GPU 旋转，`encode_ms` 降到
约 20ms；改用 RGA+MPP 后约 7.4ms，27.6fps。

**追问**：Canvas 旋转真的用 GPU 吗？
Chromium 的 2D canvas 在 GPU 合成路径下会加速；即使软件路径，一次 1M 像素旋转也比
C 里 1M 次跨步访存更连续。

### Q11：静态画面怎么省带宽？

抓帧后对 mmap 做 BLAKE2b 签名，签名相同就不编码、不发送；桌面静止时 0 KB/s。
**追问**：光标移动会破坏签名吗？
会；光标在 scanout 里，移动会改变像素，所以会出帧。

### Q12：WebSocket 背压怎么处理？

`WSClient` 发送队列 maxsize=3，满了丢最旧帧；`AudioClient` maxsize=24，同样丢旧包。
前端 `pendingFrame` 只保留最新帧。这样延迟不累积，表现为跳帧追最新。

### Q13：JPEG 质量/子采样怎么选？

q78 是清晰度/带宽折中；4:2:0 更小更快，4:2:2 文字更清晰。C 版提供 `--subsamp 420/422`。
**追问**：为什么文字会糊？
色度子采样影响彩色边缘；黑白文字主要靠亮度，但低质量量化仍会糊。

---

## 三、输入与坐标

### Q14：为什么用绝对鼠标，不用相对鼠标？

相对鼠标受指针加速/累积误差影响，无法 1:1 定位；绝对鼠标（ABS_X/Y）每次给绝对位置，
不会漂移。浏览器坐标可直接映射到屏幕。

### Q15：为什么 tablet 不行？

libinput 把手写板识别为 tablet tool，Weston 走 tablet 事件路径；桌面图标点击不生效。
绝对鼠标带 `BTN_LEFT` + `PROP_POINTER`，被识别为 pointer，Weston 走 pointer 路径。

**追问**：tablet 完全没用吗？
可以做绘图/压感，但当前目标是桌面点击，所以不用。

### Q16：uinput 创建时踩过什么坑？

1. `input_event` 在 64 位是 24 字节（两个 8 字节 timeval），不能用 `llHHi`；
2. `uinput_setup` 字段顺序是 `id` 在前、`name` 在后，写反设备名会截断；
3. `UI_ABS_SETUP` 必须带 resolution，否则 libinput 丢弃 tablet；
4. Weston 只在启动时枚举设备，输入守护必须先于 Weston 启动。

### Q17：输入跳区域/点不中怎么定位？

先分两层：

1. 设备事件是否正确？`libinput debug-events` 看 `POINTER_MOTION_ABSOLUTE` 的百分比；
2. 坐标是否正确？直接向 Unix socket 发 `move`，看光标。

最后发现两个 bug：socket 连接后 1s timeout 导致空闲断开；重连后没重发
`resize 1280×800`，守护回到默认 800×1280，比例错。

### Q18：为什么输入守护要独立进程？

- 服务端重启不重建设备，不关板端窗口；
- Python/C 两个服务共享同一套键鼠；
- 设备生命周期与 Weston 启动顺序解耦。

**追问**：重启输入守护会怎样？
会重建 uinput 并重启 Weston（否则 Weston 看不到新设备），会关闭当前窗口。

### Q19：键盘中文怎么处理？

浏览器把字符转 keycode；ASCII 正常；中文依赖板端 IME。当前镜像没有中文 IME，
所以中文不会进入板端应用。后续可在板端装 IME 或做输入法协议。

---

## 四、音频与 HTTPS

### Q20：PulseAudio monitor 是什么？

sink 的回环 source，能捕获所有播放混音后的数据；即使没有物理扬声器，也能抓到
应用播放。`parec --device=<sink>.monitor` 即可。

### Q21：虚拟麦克风怎么实现？

1. `module-null-sink` 建 `rkdesk_mic`；
2. `module-remap-source` 把 `rkdesk_mic.monitor` 包装成 `rkdesk_mic_source`；
3. `pacat` 把浏览器 PCM 写进 null sink；
4. 把默认 source 切到 `rkdesk_mic_source`，板端应用录音即可拿到。

**追问**：为什么需要 remap？
monitor 是特殊 source，remap 后是普通录音源，应用/枚举更兼容。

### Q22：服务崩了默认录音源怎么办？

启动时把原 source 写到 `/var/run/rkdesk-mic.previous`；下次启动先恢复、再清理残留
module。页面关闭/WS 断开也会 stop 并恢复。

### Q23：为什么浏览器麦克风必须 HTTPS？

`getUserMedia` 只在 secure context 可用：HTTPS 或 localhost。`http://IP` 不是安全上下文。
所以有 8088（看/听）和 8443（麦克风）两个入口。

**追问**：自签名证书浏览器不信任怎么办？
首次手动信任；或 `certutil -addstore -user Root` 导入；或 SSH 隧道用
`http://localhost:8088`。

### Q24：音频为什么用 PCM，不用 Opus？

PCM 实现简单、无编解码依赖、延迟可控；48k 立体声约 1.5 Mbps，局域网可接受。
Opus 能降带宽/延迟，但板端要编码器，WebAudio 解码也更复杂。

**追问**：延迟多少？
Pulse 抓取 30ms + WS + 浏览器 80ms 调度，约 150–250ms。

### Q25：浏览器音频怎么播放不爆音？

AudioBufferSourceNode 按 `nextTime` 调度，提前 80ms 排队；`nextTime` 落后/超前时重置，
避免 buffer 重叠或空档；音量用 GainNode。

### Q26：麦克风采集怎么处理？

`getUserMedia` → `MediaStreamAudioSourceNode` → AudioWorklet，每 960 帧（20ms）转
Int16，WebSocket 二进制发送；工作线程不阻塞 UI。

---

## 五、C 版、性能与并发

### Q27：Windows 上没有交叉编译器怎么办？

Bootlin aarch64 工具链是 Linux ELF，Git Bash 不能执行；最后用 **Zig Windows**
`zig cc -target aarch64-linux-gnu`，链接板端 `libturbojpeg/librga/GStreamer/OpenSSL`，
手写最小 ABI 声明，不需要 dev 头文件。

### Q28：C 版为什么能到 27.6fps，CPU 只有 9.9%？

- 直接 DRM mmap，无 Python 对象/解释器；
- **RGA** 把 BGRX 转 NV12（约 5ms），**MPP** 硬件 JPEG（约 7.4ms/帧）；
- 不在 C 里旋转，浏览器 GPU 旋转；
- 静止时 256B 采样哈希 + 帧间隔休眠；输入事件直接 force 出帧；
- 每客户端最新帧邮箱，慢客户端不阻塞采集；
- 无客户端暂停编码。

### Q29：C 版线程模型？有竞态吗？

main accept，每个连接一个线程；capture 线程编码；每个客户端一个 sender 线程 +
最新帧邮箱。采集用 `trylock` 写 pending，拿不到锁就丢帧；sender 独立 SSL_write。
client list 用互斥锁；删除客户端时先摘链、置 closing、join sender，再释放 SSL/fd。

### Q30：C 版点击为什么一开始无效？

C 页面发的是 Python `/ws` 的 `down/up`，但输入守护只认 `move/button`。
修复为 `move + button`，并在按下/抬起之间加 25/60ms，避免 Weston 还没应用 move
就收到 button。

### Q31：为什么 C MPP 的 encode 比 Python 低，但画质略软？

MPP 是硬件 JPEG，q85/4:2:0，`encode_ms` 约 7.4ms；Python 是 Pillow q78/4:2:2，
文字更锐但 CPU 64.8%。C 还有 Turbo fallback（19.6ms/4:2:2），可按画质/CPU 取舍。

### Q32：C 静止为什么只有 7.2% CPU？

没有输入时用 256B 采样哈希 + `1000/fps` 休眠；有输入事件时 force 跳过哈希。
无客户端时约 0–1%。早期版本静止分支 `usleep(1000)`，每秒跑几百次哈希，CPU 61%，
这是“忙等 + 重复哈希”的典型反例。

---

## 六、可靠性、安全与扩展

### Q33：开机为什么会启动两个 Weston？怎么修？

S99 启动时 S49 进程已在但 `wayland-0` socket 未就绪，S99 又 start 一次。
修复：S99 先等 socket 10s，超时才判断进程/重启；socket 是“就绪”信号。

### Q34：如何保证服务重启不关板端窗口？

输入设备在独立守护进程 S48；S99 重启只重启 Web 服务，不动 uinput/Weston。
只有重启 S48 才会重建输入设备并重启 Weston。

### Q35：安全边界是什么？

默认无认证，监听 0.0.0.0，仅可信局域网；HTTPS 是自签名，不能防中间人；
跨网用 SSH 隧道/反代/认证。输入注入等价于物理键鼠，权限很高。

### Q36：HDMI 热插拔支持吗？

Python 版每 2s 重选最大活动 CRTC，并读取 `wayland-info` transform/logical size；
HDMI 横向时 rotation=0，1920×1080 直接推流。C 版固定启动时 CRTC，不处理热插拔。

### Q37：摄像头怎么扩展？

当前无 sensor。接 USB UVC 后可用 OpenCV/`v4l2` 抓 MJPEG，走 `/api/camera.mjpg`
或 `/ws` 视频通道；浏览器摄像头回传需要 `v4l2loopback` 虚拟摄像头驱动。

### Q38：多用户/多客户端怎么处理？

视频/音频广播给多个 WS 客户端；输入共享同一套键鼠（和 VNC 一样）。
麦克风用 `mic_owner` 跟踪，owner 断开就停；更严格的多租户需要会话隔离/权限。

### Q39：如果再做一遍，会怎么改进？

- 音频换 Opus/WebRTC，降低延迟/带宽；
- 视频用 H.264 硬编码 + WebRTC；
- C 版补 HDMI 热插拔、摄像头；
- 加 token/认证/审计；
- 摄像头 MJPEG/虚拟摄像头；
- 自动化回归（四角点击、音频回环、分辨率切换）。

### Q40：这个项目最大的挑战是什么？

不是写某个功能，而是 **跨层定位**：黑屏可能是 fbdev/GBM/DRM 任一层；点击偏可能是
libinput 分类、uinput ABI、socket 状态、旋转公式任一层。方法是做能区分层的实验，
再用度量确认，而不是盲改。

---

## 白板题：60 秒画系统

1. 画五层：浏览器 → WS/HTTP → 服务 → Weston/内核 → 硬件；
2. 视频：CRTC → FB → dma-buf → mmap → JPEG → Canvas；
3. 输入：浏览器 → JSON → uinput → libinput → Weston；
4. 旋转：800×1280 → rotate-90 → 1280×800 → 逆变换；
5. 音频：sink → monitor → parec → WS → AudioContext；麦克风反向 null sink/remap；
6. C：capture thread + client threads + TurboJPEG；
7. 数字：Python 15fps/52MB/CPU64.8%；C 27.6fps/18MB/CPU9.9%；音频 150–250ms。

---

## 容易说错的点

| 不要这样说 | 更准确的说法 |
| --- | --- |
| “读 /dev/fb0 就能抓屏” | Weston 用 GBM，fb0 是模拟层，必须读 KMS CRTC |
| “dma-buf 是零拷贝” | GPU→用户态不复制整帧，但 JPEG 编码仍要读像素 |
| “WebRTC 一定更低延迟” | 取决于编码/网络/缓冲；本项目局域网 WS+JPEG 足够 |
| “tablet 和鼠标一样” | libinput 分类不同，桌面点击路径不同 |
| “HTTPS 只是加密” | 这里还承担 secure context，解锁 getUserMedia |
| “C 一定全面更快” | C 也做视频/输入/音频/HTTPS，但不处理 HDMI 热插拔/摄像头 |
| “音频是抓扬声器” | 抓的是 sink monitor 混音，不需要物理扬声器 |
