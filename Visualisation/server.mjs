import express from 'express';
import { createServer } from 'node:http';
import { WebSocketServer } from 'ws';
import net from 'node:net';
import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const app = express();
const httpServer = createServer(app);
const wsServer = new WebSocketServer({ noServer: true });
const port = Number(process.env.PORT || 5173);
const boardIp = process.env.BOARD_IP || '192.168.31.153';
const transport = process.env.TRANSPORT || 'VNC over LAN';
const vncTarget = process.env.VNC_TARGET || `${boardIp}:5900`;
const [defaultVncHost, defaultVncPortText = '5900'] = vncTarget.split(':');
const defaultVncPort = Number(defaultVncPortText);
const vncPasswordConfigured = Boolean(process.env.VNC_PASSWORD);
const [defaultStreamHost, defaultStreamPortText = '8765'] = (process.env.STREAM_TARGET || `${boardIp}:8765`).split(':');
const defaultStreamPort = Number(defaultStreamPortText);
const boardAgentUrl = process.env.BOARD_AGENT_URL || `http://${defaultStreamHost}:${defaultStreamPort}`;
const boardAgentToken = process.env.RKUI_TOKEN || '';

app.use(express.json({ limit: '256kb' }));
app.use('/vendor/novnc', express.static(join(__dirname, 'node_modules/@novnc/novnc')));
// API routes must be registered before the single-page fallback.
app.use(express.static(join(__dirname, 'web')));

app.get('/api/status', (_req, res) => {
  res.json({
    board: {
      name: 'RK3576 开发板',
      ip: boardIp,
      transport,
      target: vncTarget,
      streamTarget: `${defaultStreamHost}:${defaultStreamPort}`,
      state: '待连接',
      note: transport.includes('Weston')
        ? '板端当前是 Weston/Wayland，会话可用后走专用画面流与输入通道。'
        : '需要板端先运行 VNC 服务，并提供当前桌面会话。'
    },
    host: {
      console: `http://localhost:${port}`,
      vncPasswordConfigured,
      mode: 'browser-console'
    },
    capabilities: transport.includes('Weston')
      ? ['Wayland/Weston 画面流', '鼠标与键盘事件转发', '全屏与缩放', '低带宽画质调节']
      : ['浏览器查看板端桌面', '鼠标与键盘事件转发', '全屏与缩放', '低带宽画质调节']
  });
});

app.get('/healthz', (_req, res) => res.json({ ok: true }));

app.get('/api/board-health', async (_req, res) => {
  try {
    const response = await fetch(`${boardAgentUrl}/healthz`, {
      headers: boardAgentToken ? { Authorization: `Bearer ${boardAgentToken}` } : undefined,
      signal: AbortSignal.timeout(1800)
    });
    const data = await response.json();
    res.status(response.status).json({ ...data, agentUrl: boardAgentUrl });
  } catch (error) {
    res.status(502).json({ ok: false, agentUrl: boardAgentUrl, error: error.message });
  }
});

app.get('/api/board-frame', async (_req, res) => {
  try {
    const response = await fetch(`${boardAgentUrl}/frame.jpg`, {
      headers: boardAgentToken ? { Authorization: `Bearer ${boardAgentToken}` } : undefined,
      signal: AbortSignal.timeout(3500)
    });
    if (!response.ok) return res.status(response.status).send(await response.text());
    res.setHeader('Content-Type', response.headers.get('content-type') || 'image/jpeg');
    res.setHeader('Cache-Control', 'no-store');
    res.send(Buffer.from(await response.arrayBuffer()));
  } catch (error) {
    res.status(502).json({ ok: false, agentUrl: boardAgentUrl, error: error.message });
  }
});

app.post('/api/board-input', async (req, res) => {
  try {
    const response = await fetch(`${boardAgentUrl}/input`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(boardAgentToken ? { Authorization: `Bearer ${boardAgentToken}` } : {}) },
      body: JSON.stringify(req.body),
      signal: AbortSignal.timeout(1800)
    });
    res.status(response.status).json(await response.json());
  } catch (error) {
    res.status(502).json({ ok: false, agentUrl: boardAgentUrl, error: error.message });
  }
});

app.get('/*splat', (req, res) => {
  if (req.path.startsWith('/api/')) return res.status(404).json({ error: 'API route not found' });
  const index = join(__dirname, 'web/index.html');
  if (!existsSync(index)) return res.status(404).send('Web UI not found');
  res.sendFile(index);
});

httpServer.on('upgrade', (request, socket, head) => {
  const url = new URL(request.url, `http://${request.headers.host}`);
  if (url.pathname !== '/websockify') {
    socket.destroy();
    return;
  }
  wsServer.handleUpgrade(request, socket, head, (client) => {
    const host = url.searchParams.get('host') || defaultVncHost;
    const port = Number(url.searchParams.get('port') || defaultVncPort);
    const upstream = net.createConnection({ host, port });
    let closed = false;
    const closeBoth = () => {
      if (closed) return;
      closed = true;
      client.close();
      upstream.destroy();
    };

    client.on('message', (data, isBinary) => {
      if (isBinary && !upstream.destroyed) upstream.write(data);
    });
    client.on('close', closeBoth);
    client.on('error', closeBoth);
    upstream.on('connect', () => {
      client.binaryType = 'arraybuffer';
    });
    upstream.on('data', (data) => {
      if (client.readyState === 1) client.send(data, { binary: true });
    });
    upstream.on('error', (error) => {
      if (client.readyState === 1) {
        client.send(JSON.stringify({ type: 'error', message: `无法连接 VNC ${host}:${port}：${error.message}` }));
      }
      closeBoth();
    });
    upstream.on('close', closeBoth);
  });
});

httpServer.listen(port, '127.0.0.1', () => {
  console.log(`RK3576 remote console: http://localhost:${port}`);
  console.log(`VNC target: ${vncTarget}`);
});
