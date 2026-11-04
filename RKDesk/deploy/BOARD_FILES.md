# 板端文件与仓库对应关系

`RKDesk` 的板端脚本全部纳入仓库，`deploy.sh` 会按下面的映射安装。
可用 `bash tools/verify_board.sh root@192.168.31.153` 逐文件校验 MD5。

| 板端路径 | 仓库来源 | 说明 |
| --- | --- | --- |
| `/usr/bin/weston` | `deploy/weston` | 原 ELF 丢失后的 `wet_main` 启动器 |
| `/etc/init.d/S48rkdesk-input` | `deploy/S48rkdesk-input` | 创建绝对鼠标/键盘，早于 Weston |
| `/etc/init.d/S49weston` | `deploy/S49weston` | 修复停止逻辑的 Weston init |
| `/etc/init.d/S99rkdesk` | `deploy/S99rkdesk` | 抓屏 + WebSocket 服务 |
| `/etc/xdg/weston/weston.ini.d/99-rkdesk-rotate.ini` | `deploy/99-rkdesk-rotate.ini` | DSI rotate-90，1280×800 横向 |
| `/opt/rkdesk/rkdesk_server.py` | `board/rkdesk_server.py` | Web 服务入口 |
| `/opt/rkdesk/rkdesk_input.py` | `board/rkdesk_input.py` | 输入守护进程 |
| `/opt/rkdesk/rkdesk/*.py` | `board/rkdesk/*.py` | 抓屏、输入、音频、虚拟桌面等模块 |
| `/opt/rkdesk/web/*` | `web/*` | 浏览器客户端（含 `av.js` 音频） |
| `/opt/rkdesk/rkdesk_c` | `c-src/rkdesk_c`（本地编译，不入库） | C 对比服务（8089），手动启动 |
| `/opt/rkdesk/certs/rkdesk.crt` | `deploy/certs/`（部署时生成） | HTTPS 自签名证书，不提交仓库 |
| `/opt/rkdesk/certs/rkdesk.key` | `deploy/certs/`（部署时生成） | 私钥，权限 600，不提交仓库 |

## 安装

```bash
cd RKDesk
bash deploy.sh                 # 默认 root@192.168.31.153
```

部署脚本会：

1. 用 tar over SSH 复制 `board/` 和 `web/` 到 `/opt/rkdesk`；
2. 安装 `deploy/weston`、`S48`、`S49`、`S99` 和旋转配置；
3. 重启输入守护进程和 Web 服务（输入守护重启会连带重启 Weston）；
4. 启动 HTTP `8088` 与 HTTPS `8443`（证书存在时）。

HTTPS 使用自签名证书，SAN 为部署目标 IP；首次访问需在浏览器里手动信任。
浏览器麦克风必须走 HTTPS（或 localhost），HTTP 8088 只用于看画面/听声音。

## 校验

```bash
bash tools/verify_board.sh root@192.168.31.153
```

输出每个文件的 `OK / DIFF`，任意文件不一致时退出码为 1。
