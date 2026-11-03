const state = {
  api: null,
  connected: false,
  mode: null,
  viewer: null,
  streamTimer: null,
  streamBusy: false,
  lastPointer: null
};

const $ = (selector) => document.querySelector(selector);

function setStatus(label, live = false) {
  $('#connection-label').textContent = label;
  $('#link-state').textContent = live ? '已连接' : '等待板端代理';
  document.querySelector('.connection-badge .dot').classList.toggle('live', live);
  document.querySelector('.live-indicator .dot').classList.toggle('live', live);
}

function showError(message) {
  const box = $('#error-box');
  box.hidden = !message;
  box.textContent = message || '';
}

function setBusy(busy) {
  for (const button of [$('#connect-btn'), $('#empty-connect-btn')]) {
    button.disabled = busy;
    button.textContent = busy ? '正在准备…' : button.dataset.defaultText;
  }
}

async function loadStatus() {
  try {
    const response = await fetch('/api/status');
    state.api = await response.json();
    $('#resolution').textContent = state.api.board.transport.includes('Weston')
      ? state.api.board.streamTarget
      : state.api.board.target;
  } catch {
    showError('主机端服务未响应，请确认控制台服务器已经启动。');
  }
}

async function boardHealth() {
  const response = await fetch('/api/board-health', { cache: 'no-store' });
  if (!response.ok) throw new Error((await response.json()).error || '板端代理未响应');
  return response.json();
}

function startFrameStream() {
  const image = $('#board-frame');
  const tick = async () => {
    if (state.streamBusy || state.mode !== 'frame') return;
    state.streamBusy = true;
    try {
      const response = await fetch(`/api/board-frame?t=${Date.now()}`, { cache: 'no-store' });
      if (!response.ok) throw new Error('画面帧读取失败');
      const blob = await response.blob();
      const nextUrl = URL.createObjectURL(blob);
      const oldUrl = image.dataset.objectUrl;
      image.src = nextUrl;
      image.dataset.objectUrl = nextUrl;
      if (oldUrl) URL.revokeObjectURL(oldUrl);
      $('#latency').textContent = `画面流 · ${Math.round(blob.size / 1024)} KB`;
    } catch (error) {
      showError(`画面流中断：${error.message}`);
      stopFrameStream();
      setStatus('已断开');
    } finally {
      state.streamBusy = false;
    }
  };
  stopFrameStream();
  state.streamTimer = window.setInterval(tick, 120);
  tick();
}

function stopFrameStream() {
  if (state.streamTimer) window.clearInterval(state.streamTimer);
  state.streamTimer = null;
  state.streamBusy = false;
}

async function sendInput(event) {
  if (state.mode !== 'frame') return;
  try {
    await fetch('/api/board-input', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(event),
      keepalive: true
    });
  } catch {
    // The next frame request will surface a persistent connection failure.
  }
}

function bindFrameInput() {
  const image = $('#board-frame');
  image.addEventListener('mousemove', (event) => {
    if (!$('#mouse-toggle').checked) return;
    const previous = state.lastPointer || { x: event.clientX, y: event.clientY };
    state.lastPointer = { x: event.clientX, y: event.clientY };
    sendInput({ type: 'move', dx: Math.round(event.clientX - previous.x), dy: Math.round(event.clientY - previous.y) });
  });
  image.addEventListener('mouseleave', () => { state.lastPointer = null; });
  image.addEventListener('mousedown', (event) => {
    if (!$('#mouse-toggle').checked) return;
    event.preventDefault();
    sendInput({ type: 'button', button: event.button === 2 ? 'right' : event.button === 1 ? 'middle' : 'left', down: true });
  });
  image.addEventListener('mouseup', (event) => {
    if (!$('#mouse-toggle').checked) return;
    event.preventDefault();
    sendInput({ type: 'button', button: event.button === 2 ? 'right' : event.button === 1 ? 'middle' : 'left', down: false });
  });
  image.addEventListener('contextmenu', (event) => event.preventDefault());
  window.addEventListener('keydown', (event) => {
    if (state.mode !== 'frame' || !$('#keyboard-toggle').checked) return;
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName)) return;
    event.preventDefault();
    sendInput({ type: 'key', key: event.key, down: true });
  });
  window.addEventListener('keyup', (event) => {
    if (state.mode !== 'frame' || !$('#keyboard-toggle').checked) return;
    event.preventDefault();
    sendInput({ type: 'key', key: event.key, down: false });
  });
}

function connectVnc() {
  return import('/vendor/novnc/core/rfb.js').then(({ default: RFB }) => {
    const target = state.api?.board?.target || '192.168.31.153:5900';
    const [host, port = '5900'] = target.split(':');
    const websocketUrl = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/websockify?host=${encodeURIComponent(host)}&port=${encodeURIComponent(port)}`;
    const container = $('#novnc-container');
    container.hidden = false;
    $('#empty-state').hidden = true;
    state.viewer = new RFB(container, websocketUrl, { shared: true, clipViewport: false, resizeSession: false });
    state.viewer.scaleViewport = $('#fit-toggle').checked;
    state.viewer.showDotCursor = true;
    state.viewer.addEventListener('connect', () => {
      state.connected = true;
      setBusy(false);
      setStatus('已连接', true);
      $('#resolution').textContent = `${target} · VNC`;
    });
    state.viewer.addEventListener('disconnect', () => {
      state.connected = false;
      container.hidden = true;
      $('#empty-state').hidden = false;
      setStatus('已断开');
      showError('无法连接板端 VNC。当前板端是 Weston/Wayland，建议启动板端代理。');
    });
    state.viewer.addEventListener('credentialsrequired', () => {
      const password = window.prompt('请输入板端 VNC 密码');
      if (password) state.viewer.sendCredentials({ password });
    });
  });
}

async function connectBoard() {
  if (state.connected) return;
  setBusy(true);
  showError('');
  setStatus('连接中…');
  try {
    const health = await boardHealth();
    state.mode = 'frame';
    state.connected = true;
    $('#empty-state').hidden = true;
    $('#board-frame').hidden = false;
    $('#stream-overlay').hidden = false;
    $('#novnc-container').hidden = true;
    $('#resolution').textContent = `${health.width} × ${health.height} · JPEG`;
    setStatus('已连接', true);
    startFrameStream();
  } catch (agentError) {
    try {
      state.mode = 'vnc';
      await connectVnc();
    } catch (error) {
      state.mode = null;
      setBusy(false);
      $('#empty-state').hidden = false;
      setStatus('待连接');
      showError(`板端代理未响应：${agentError.message}。VNC 备用连接也失败：${error.message}`);
    }
  }
}

function bindControls() {
  $('#connect-btn').dataset.defaultText = $('#connect-btn').textContent;
  $('#empty-connect-btn').dataset.defaultText = $('#empty-connect-btn').textContent;
  $('#connect-btn').addEventListener('click', connectBoard);
  $('#empty-connect-btn').addEventListener('click', connectBoard);
  $('#refresh-btn').addEventListener('click', () => { if (state.mode === 'frame') startFrameStream(); });
  $('#fit-btn').addEventListener('click', () => {
    $('#fit-toggle').checked = true;
    if (state.viewer) state.viewer.scaleViewport = true;
  });
  $('#fullscreen-btn').addEventListener('click', async () => {
    const viewer = $('#viewer');
    if (!document.fullscreenElement) await viewer.requestFullscreen?.();
    else await document.exitFullscreen?.();
  });
  $('#fit-toggle').addEventListener('change', (event) => {
    if (state.viewer) state.viewer.scaleViewport = event.target.checked;
  });
  $('#mouse-toggle').addEventListener('change', (event) => {
    if (state.viewer) state.viewer.viewOnly = !event.target.checked;
  });
}

await loadStatus();
bindFrameInput();
bindControls();
