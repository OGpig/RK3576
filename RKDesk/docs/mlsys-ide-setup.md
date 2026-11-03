# mlsysbook · IDE Setup 实验（RK3576 版）

对应页面：<https://mlsysbook.ai/kits/contents/ide-setup.html>

官方页面以 Raspberry Pi / Arduino 为示例，核心目标是四件事：

1. 开发机具备 Git、Python、VS Code；
2. 能通过 SSH 登录板子；
3. 能在 IDE 里远程编辑/运行代码；
4. 验证 Python 与视觉库可导入，摄像头等外设可枚举。

RK3576（TL3576-EVM，Buildroot 2024.02）没有 apt、没有桌面、没有接显示器，
下面给出逐项对应的做法。**不需要额外购买显示器/键鼠**——用 RKDesk 代替本地控制台。

---

## 0. 开发机（Windows）准备

| 项目 | 要求 | 本机状态 |
| --- | --- | --- |
| 操作系统 | Windows 10/11 | ✅ |
| Git | 任意较新版本 | ✅ `git --version` |
| Python | 3.8+（仅本机脚本用） | 建议安装 |
| VS Code | 1.80+ | 未在默认路径检测到，需安装 |
| 网络 | 与板子同一局域网 | ✅ 192.168.31.0/24 |

VS Code 下载：<https://code.visualstudio.com/>

必装扩展：**Remote - SSH**（`ms-vscode-remote.remote-ssh`）。

---

## 1. SSH 登录（已完成）

已在 `C:\Users\ADMIN\.ssh\config` 写入：

```sshconfig
Host rk3576
    HostName 192.168.31.153
    User root
    Port 22
    StrictHostKeyChecking accept-new
    ServerAliveInterval 30
    ServerAliveCountMax 3
```

验证：

```bash
ssh rk3576 "uname -a; python3 -V"
```

期望：

```text
Linux RK3576-Tronlong 6.1.115-rt16-... aarch64 GNU/Linux
Python 3.11.8
```

板端 `sshd` 为 OpenSSH 9.6p1，root 使用现有密钥免密登录。

> 串口不是必需的。官方页面的「串口调试」主要针对 Arduino；RK3576 用网线 + SSH 即可。

---

## 2. VS Code Remote-SSH

1. 安装 VS Code 与 Remote - SSH 扩展；
2. `F1` → `Remote-SSH: Connect to Host...` → 选择 `rk3576`；
3. 首次连接会在板端下载 VS Code Server（约 120 MB，aarch64）；
4. 连上后 `File → Open Folder` → `/root`。

板端依赖已核对：

| 依赖 | 状态 |
| --- | --- |
| glibc | 2.38（≥ 2.28） |
| libstdc++ | 6.0.30 |
| bash / tar / wget / curl / setsid | 均存在 |
| 可用磁盘 | 4.6 GB |

如果 VS Code Server 因 Buildroot 精简依赖启动失败，用下面任一替代方案：

**方案 A：sshfs-win 挂载**

```powershell
winget install SSHFS-Win.SSHFS-Win
net use Z: \\sshfs\rk3576\root
```

之后用本地 VS Code 打开 `Z:\`，保存即写回板端。

**方案 B：scp / rsync 同步**

```bash
scp -r rk3576:/root/myproject ./
# 改完再传回去
scp -r ./myproject rk3576:/root/
```

---

## 3. Python 环境

板端已经预装了大量库，**不建议无脑重装**：

```text
Python 3.11.8
numpy 1.26.4      cv2 5.0.0        Pillow 13.0.0
onnxruntime 1.31  torch 2.2.0      rknnlite / rknn
psutil 7.2.2      requests 2.31
```

官方页面的 `python3 -m venv` 在板端可用。若要让 venv 继承系统里已有的
numpy/OpenCV，加 `--system-site-packages`：

```bash
ssh rk3576
python3 -m venv --system-site-packages ~/ml_projects
source ~/ml_projects/bin/activate
python -c "import numpy, cv2; print(numpy.__version__, cv2.__version__)"
```

如果要装新包，板端能直连外网，但默认 PyPI 较慢，建议阿里云镜像：

```bash
pip install -i https://mirrors.aliyun.com/pypi/simple/ <package>
```

> 注意：venv 不加 `--system-site-packages` 时看不到系统包，需要重新 pip 安装，
> 在 4 GB 内存 / 16 GB 存储的板子上比较费空间。

---

## 4. 环境验证（已执行）

把 `RKDesk/tools/verify_env.py` 传到板端运行，或直接：

```bash
ssh rk3576 "python3 -" < RKDesk/tools/verify_env.py
```

本次实测结果（完整输出见 `docs/env-report.txt`）：

```text
Python 3.11.8
numpy 1.26.4 / cv2 5.0.0 / PIL 13.0.0 / onnxruntime 1.31.0
torch 2.2.0 / rknnlite / rknn / psutil 7.2.2 / requests 2.31.0
/dev/fb0 /dev/dri/card0 /dev/dri/renderD128 /dev/uinput /dev/video-camera0 /dev/snd/controlC0 均存在
RKDesk http://<board>:8088 OK 1280x800 shell=True
summary: 17 passed, 1 warning
```

唯一 warning 是 `flask` 未安装，本实验不需要。

官方页面里的两条验证命令对应到 RK3576：

| 官方（Raspberry Pi） | RK3576 对应 |
| --- | --- |
| `libcamera-hello --timeout 5000` | `v4l2-ctl --list-devices`（当前无摄像头，输出为空） |
| `python3 -c "import tensorflow..."` | `python3 -c "import numpy, cv2, rknnlite; print('ok')"` |
| `python3 -c "import cv2..."` | 同上 |

---

## 5. 没有显示器 / 摄像头 / 耳机怎么办

| 缺失 | 影响 | 替代方案 |
| --- | --- | --- |
| HDMI/DSI 显示器 | 看不到板端 GUI | **RKDesk**：浏览器打开 `http://192.168.31.153:8088` |
| USB 键鼠 | 无法本地操作 | RKDesk 把浏览器鼠标/键盘事件回传板端 |
| 摄像头 | 无法做图像采集实验 | 当前 `/dev/video*` 是 ISP/编解码节点，无 sensor；后续插 USB 摄像头即可 |
| 耳机/扬声器 | 无法听音频 | 板端有 ES8388/HDMI 声卡节点；可先用文件方式处理音频，或接 USB 声卡 |

RKDesk 的终端就是板端真实 shell，因此 **IDE 实验里的所有命令都可以直接在
浏览器终端里执行**，不需要额外显示器。

```bash
# 在 RKDesk 终端里
uname -a
free -h
df -h
python3 -c "import numpy, cv2, rknnlite; print('ML stack ok')"
```

---

## 6. 验收清单

- [x] 开发机能 `ssh rk3576` 免密登录
- [x] 板端 Python 3.11.8，numpy/OpenCV/ONNX/RKNN 可导入
- [x] 板端 4.6 GB 可用磁盘、1.9 GB 可见内存（另有 2 GB swapfile）
- [x] 无显示器也能看到并操作板端桌面（RKDesk，`http://192.168.31.153:8088`）
- [x] 浏览器终端可执行命令、支持中文与颜色
- [ ] VS Code Remote-SSH 首次连接（需本机安装 VS Code 后执行）
- [ ] 外接 USB 摄像头后的 `v4l2-ctl --list-devices` 验证（当前无硬件）

---

## 7. 常见问题

**Q: `ssh rk3576` 提示 Host key 变更？**
板子重刷后 host key 会变，执行 `ssh-keygen -R 192.168.31.153` 再连。

**Q: VS Code 一直卡在 "Setting up SSH Host"？**
在板端看 `~/.vscode-server/` 是否在下载；Buildroot 的 `/tmp` 是 tmpfs，
空间足够；若 glibc 报错，改用 sshfs-win。

**Q: 浏览器打开 8088 空白/一直转圈？**
先 `ssh rk3576 "/etc/init.d/S99rkdesk status"`，再 `tail -50 /var/log/rkdesk.log`。
端口被占用时改 `RKDESK_PORT=8089 /etc/init.d/S99rkdesk restart`。

**Q: 终端里中文显示为方框？**
RKDesk 使用板端 `SourceHanSansCN` 字体，已实测中文正常；如果是 SSH 终端
（如 Windows Terminal），需本地字体支持。

**Q: 想改分辨率/帧率？**
编辑 `/etc/init.d/S99rkdesk` 里的 `--fps/--quality`，或启动时加
`--width 1024 --height 768`。改完 `restart`。
