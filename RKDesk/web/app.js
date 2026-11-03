(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const canvas = $("screen");
  const ctx = canvas.getContext("2d", { alpha: false, desynchronized: true });
  const stage = $("stage");
  const viewport = $("viewport");
  const overlay = $("overlay");
  const overlayTitle = $("overlay-title");
  const overlayText = $("overlay-text");
  const dot = $("dot");
  const stateText = $("state");
  const latencyText = $("latency");
  const fpsText = $("fps");
  const bandwidthText = $("bandwidth");
  const qualitySelect = $("quality");
  const kbd = $("kbd");

  const info = {
    mode: $("infoMode"), res: $("infoRes"), host: $("infoHost"), ip: $("infoIp"),
    uptime: $("infoUptime"), disk: $("infoDisk"), cpu: $("infoCpu"), mem: $("infoMem"),
    temp: $("infoTemp"), bandwidth: $("infoBandwidth"), latency: $("infoLatency"),
    fps: $("infoFps"), quality: $("infoQuality")
  };

  let socket = null;
  let connected = false;
  let attempts = 0;
  let reconnectTimer = null;
  let latencyTimer = null;
  let serverWidth = 1280;
  let serverHeight = 800;
  let isMirror = true;

  let pendingFrame = null;
  let decoding = false;
  let frameCount = 0;
  let lastFpsAt = performance.now();
  let byteCount = 0;
  let lastBytesAt = performance.now();
  let currentFps = 0;
  let currentKbps = 0;

  let pendingMove = null;
  let moveScheduled = false;

  let composing = false;
  let ignoreInputUntil = 0;
  let zoomMode = "fit";

  const SPECIAL = new Set([
    "Enter", "Backspace", "Tab", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft",
    "ArrowRight", "Home", "End", "Delete", "PageUp", "PageDown", "Insert",
    "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12"
  ]);

  // ------------------------------------------------------------------ layout
  function fitCanvas() {
    const style = window.getComputedStyle(viewport);
    const padX = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
    const padY = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);
    const availableW = Math.max(160, viewport.clientWidth - padX - 14);
    const availableH = Math.max(120, viewport.clientHeight - padY - 14);
    viewport.classList.toggle("actual", zoomMode === "actual");

    if (zoomMode === "actual") {
      canvas.style.width = canvas.width + "px";
      canvas.style.height = canvas.height + "px";
      return;
    }
    const scale = Math.min(availableW / Math.max(1, canvas.width), availableH / Math.max(1, canvas.height));
    canvas.style.width = Math.round(canvas.width * scale) + "px";
    canvas.style.height = Math.round(canvas.height * scale) + "px";
  }

  function setFrameSize(width, height) {
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
      fitCanvas();
    }
    info.res.textContent = width + "×" + height;
  }

  function setZoom(mode) {
    zoomMode = mode;
    $("fit").classList.toggle("active", mode === "fit");
    $("actual").classList.toggle("active", mode === "actual");
    fitCanvas();
  }

  // ------------------------------------------------------------------ frames
  async function decodeFrame(buffer) {
    if (decoding) {
      pendingFrame = buffer;
      return;
    }
    decoding = true;
    try {
      const blob = new Blob([buffer], { type: "image/jpeg" });
      let painted = false;
      if (window.createImageBitmap) {
        try {
          const bitmap = await createImageBitmap(blob);
          setFrameSize(bitmap.width, bitmap.height);
          ctx.drawImage(bitmap, 0, 0);
          bitmap.close();
          painted = true;
        } catch (error) {
          painted = false;
        }
      }
      if (!painted) {
        await new Promise((resolve, reject) => {
          const image = new Image();
          image.onload = () => {
            setFrameSize(image.width, image.height);
            ctx.drawImage(image, 0, 0);
            URL.revokeObjectURL(image.src);
            resolve();
          };
          image.onerror = reject;
          image.src = URL.createObjectURL(blob);
        });
      }
      frameCount += 1;
      byteCount += buffer.byteLength;
    } catch (error) {
      // Corrupt frame: drop it, the next one replaces it.
    } finally {
      decoding = false;
      if (pendingFrame) {
        const next = pendingFrame;
        pendingFrame = null;
        decodeFrame(next);
      }
    }
  }

  // ----------------------------------------------------------------- network
  function send(message) {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(message));
    }
  }

  function setStatus(mode, text) {
    connected = mode === "online";
    dot.className = "dot " + (connected ? "online" : mode === "warn" ? "" : "offline");
    stateText.textContent = text;
  }

  function showOverlay(title, text, retryVisible) {
    overlayTitle.textContent = title;
    overlayText.textContent = text;
    overlay.classList.remove("hidden");
    $("retry").style.display = retryVisible ? "" : "none";
  }

  function hideOverlay() {
    overlay.classList.add("hidden");
  }

  function connect() {
    clearTimeout(reconnectTimer);
    if (socket) {
      try { socket.close(); } catch (error) { /* ignore */ }
    }
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    const token = new URLSearchParams(location.search).get("token");
    const url = protocol + "://" + location.host + "/ws" + (token ? "?token=" + encodeURIComponent(token) : "");
    setStatus("warn", "连接中…");
    showOverlay("正在连接开发板", location.host + " · 等待板端画面", false);

    socket = new WebSocket(url);
    socket.binaryType = "arraybuffer";

    socket.onopen = () => setStatus("warn", "握手中…");

    socket.onmessage = (event) => {
      if (typeof event.data === "string") {
        let message;
        try { message = JSON.parse(event.data); } catch (error) { return; }
        if (message.t === "hello") {
          serverWidth = message.width || serverWidth;
          serverHeight = message.height || serverHeight;
          setFrameSize(serverWidth, serverHeight);
          fitCanvas();
          hideOverlay();
          isMirror = message.mode === "mirror";
          document.body.classList.toggle("mirror", isMirror);
          info.mode.textContent = isMirror ? "真实屏幕镜像" : "虚拟桌面";
          if (message.host) info.host.textContent = message.host;
          if (message.ip) info.ip.textContent = message.ip;
          setStatus("online", "已连接 · " + serverWidth + "×" + serverHeight + (isMirror ? " · 真实屏幕" : ""));
          startLatency();
        } else if (message.t === "pong") {
          const rtt = performance.now() - message.ts;
          latencyText.textContent = rtt.toFixed(0) + " ms";
          info.latency.textContent = rtt.toFixed(0) + " ms";
        } else if (message.t === "stats") {
          updateStats(message);
        }
        return;
      }
      decodeFrame(event.data);
    };

    socket.onclose = () => {
      stopLatency();
      setStatus("offline", "已断开");
      const delay = Math.min(8, 1 + attempts);
      attempts += 1;
      showOverlay("连接已断开", "板端 " + location.host + " · " + delay + " 秒后重试", true);
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(connect, delay * 1000);
    };

    socket.onerror = () => setStatus("offline", "连接失败");
  }

  function startLatency() {
    stopLatency();
    latencyTimer = setInterval(() => send({ t: "ping", ts: performance.now() }), 2000);
    send({ t: "ping", ts: performance.now() });
  }

  function stopLatency() {
    clearInterval(latencyTimer);
    latencyTimer = null;
  }

  function updateStats(message) {
    const cpu = Number(message.cpu || 0);
    const mem = Number(message.mem || 0);
    const temp = Number(message.temp || 0);
    $("cpuBar").style.width = Math.min(100, cpu) + "%";
    $("memBar").style.width = Math.min(100, mem) + "%";
    $("tempBar").style.width = Math.min(100, temp / 90 * 100) + "%";
    info.cpu.textContent = cpu.toFixed(0) + "%";
    info.mem.textContent = mem.toFixed(0) + "%";
    info.temp.textContent = temp.toFixed(0) + " °C";
    if (message.uptime) info.uptime.textContent = message.uptime;
    if (message.disk_used !== undefined) {
      info.disk.textContent = message.disk_used + " / " + message.disk_total + " GB";
    }
    if (message.host) info.host.textContent = message.host;
    if (message.ip) info.ip.textContent = message.ip;
  }

  // ------------------------------------------------------------------- mouse
  function toServer(event) {
    const rect = canvas.getBoundingClientRect();
    const x = Math.round((event.clientX - rect.left) / rect.width * canvas.width);
    const y = Math.round((event.clientY - rect.top) / rect.height * canvas.height);
    return {
      x: Math.max(0, Math.min(canvas.width - 1, x)),
      y: Math.max(0, Math.min(canvas.height - 1, y))
    };
  }

  function queueMove(position) {
    pendingMove = position;
    if (moveScheduled) return;
    moveScheduled = true;
    requestAnimationFrame(() => {
      moveScheduled = false;
      if (pendingMove) {
        send({ t: "move", x: pendingMove.x, y: pendingMove.y });
        pendingMove = null;
      }
    });
  }

  canvas.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    canvas.setPointerCapture(event.pointerId);
    kbd.focus({ preventScroll: true });
    const position = toServer(event);
    send({ t: "down", x: position.x, y: position.y, b: event.button });
  });

  canvas.addEventListener("pointermove", (event) => queueMove(toServer(event)));

  canvas.addEventListener("pointerup", (event) => {
    event.preventDefault();
    const position = toServer(event);
    send({ t: "up", x: position.x, y: position.y, b: event.button });
  });

  canvas.addEventListener("pointercancel", (event) => {
    const position = toServer(event);
    send({ t: "up", x: position.x, y: position.y, b: event.button });
  });

  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    const position = toServer(event);
    send({ t: "wheel", x: position.x, y: position.y, dy: Math.sign(event.deltaY) });
  }, { passive: false });

  canvas.addEventListener("contextmenu", (event) => event.preventDefault());
  canvas.addEventListener("dblclick", (event) => event.preventDefault());
  canvas.addEventListener("pointerdown", () => {
    setTimeout(() => kbd.focus({ preventScroll: true }), 0);
  });

  // ---------------------------------------------------------------- keyboard
  function sendKey(event) {
    send({
      t: "key", key: event.key, code: event.code,
      ctrl: event.ctrlKey, alt: event.altKey, shift: event.shiftKey, meta: event.metaKey
    });
  }

  kbd.addEventListener("keydown", (event) => {
    if (event.isComposing) return;
    const modifier = event.ctrlKey || event.altKey || event.metaKey;
    if (SPECIAL.has(event.key) || modifier) {
      sendKey(event);
      event.preventDefault();
    }
  });

  kbd.addEventListener("input", () => {
    if (composing) return;
    if (performance.now() < ignoreInputUntil) {
      kbd.value = "";
      return;
    }
    const text = kbd.value;
    kbd.value = "";
    if (text) send({ t: "text", text: text });
  });

  kbd.addEventListener("compositionstart", () => { composing = true; });
  kbd.addEventListener("compositionend", (event) => {
    composing = false;
    kbd.value = "";
    if (event.data) {
      send({ t: "text", text: event.data });
      ignoreInputUntil = performance.now() + 80;
    }
  });

  kbd.addEventListener("paste", (event) => {
    event.preventDefault();
    const text = (event.clipboardData || window.clipboardData).getData("text");
    if (text) send({ t: "paste", text: text });
  });

  // ------------------------------------------------------------------ toolbar
  qualitySelect.addEventListener("change", () => {
    const value = Number(qualitySelect.value);
    localStorage.setItem("rkdesk.quality", String(value));
    info.quality.textContent = String(value);
    send({ t: "quality", value: value });
  });
  const savedQuality = localStorage.getItem("rkdesk.quality");
  if (savedQuality && qualitySelect.querySelector('option[value="' + savedQuality + '"]')) {
    qualitySelect.value = savedQuality;
    info.quality.textContent = savedQuality;
  }

  $("fit").addEventListener("click", () => setZoom("fit"));
  $("actual").addEventListener("click", () => setZoom("actual"));

  $("panelToggle").addEventListener("click", () => {
    document.body.classList.toggle("panel-open");
    setTimeout(fitCanvas, 320);
  });
  $("panelClose").addEventListener("click", () => {
    document.body.classList.remove("panel-open");
    setTimeout(fitCanvas, 320);
  });

  $("retry").addEventListener("click", () => { attempts = 0; connect(); });
  $("restart").addEventListener("click", () => { attempts = 0; connect(); });

  $("full").addEventListener("click", async () => {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await document.documentElement.requestFullscreen();
    } catch (error) { /* ignore */ }
    setTimeout(fitCanvas, 140);
  });

  $("shot").addEventListener("click", () => {
    canvas.toBlob((blob) => {
      if (!blob) return;
      const link = document.createElement("a");
      const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
      link.download = "rk3576-" + stamp + ".png";
      link.href = URL.createObjectURL(blob);
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 2000);
    }, "image/png");
  });

  $("paste").addEventListener("click", async () => {
    let text = "";
    try {
      text = await navigator.clipboard.readText();
    } catch (error) {
      text = window.prompt("本机剪贴板不可读，请手动粘贴文本：") || "";
    }
    if (text) send({ t: "paste", text: text });
  });

  document.addEventListener("fullscreenchange", () => setTimeout(fitCanvas, 90));
  window.addEventListener("resize", fitCanvas);
  if (window.ResizeObserver) new ResizeObserver(fitCanvas).observe(viewport);

  // -------------------------------------------------------------------- stats
  setInterval(() => {
    const now = performance.now();
    currentFps = frameCount * 1000 / Math.max(1, now - lastFpsAt);
    currentKbps = byteCount / 1024 / Math.max(0.2, (now - lastBytesAt) / 1000);
    const fpsLabel = currentFps < 0.5 ? "静止" : currentFps.toFixed(0) + " fps";
    fpsText.textContent = fpsLabel;
    info.fps.textContent = fpsLabel;
    bandwidthText.textContent = currentKbps.toFixed(0) + " KB/s";
    info.bandwidth.textContent = currentKbps.toFixed(0) + " KB/s";
    frameCount = 0;
    byteCount = 0;
    lastFpsAt = now;
    lastBytesAt = now;
  }, 1000);

  fitCanvas();
  connect();
})();
