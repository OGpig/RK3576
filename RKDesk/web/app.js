(function () {
  "use strict";

  const canvas = document.getElementById("screen");
  const ctx = canvas.getContext("2d", { alpha: false, desynchronized: true });
  const stage = document.getElementById("stage");
  const overlay = document.getElementById("overlay");
  const overlayTitle = document.getElementById("overlay-title");
  const overlayText = document.getElementById("overlay-text");
  const dot = document.getElementById("dot");
  const stateText = document.getElementById("state");
  const latencyText = document.getElementById("latency");
  const fpsText = document.getElementById("fps");
  const bandwidthText = document.getElementById("bandwidth");
  const qualitySelect = document.getElementById("quality");
  const kbd = document.getElementById("kbd");

  let socket = null;
  let connected = false;
  let attempts = 0;
  let reconnectTimer = null;
  let serverWidth = 1280;
  let serverHeight = 800;

  let pendingFrame = null;
  let decoding = false;
  let frameCount = 0;
  let lastFpsAt = performance.now();
  let byteCount = 0;
  let lastBytesAt = performance.now();
  let latencyTimer = null;

  let pendingMove = null;
  let moveScheduled = false;

  let composing = false;
  let ignoreInputUntil = 0;

  const SPECIAL = new Set([
    "Enter", "Backspace", "Tab", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft",
    "ArrowRight", "Home", "End", "Delete", "PageUp", "PageDown", "Insert",
    "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12"
  ]);

  // ------------------------------------------------------------------ frames
  function fitCanvas() {
    const rect = stage.getBoundingClientRect();
    const availableW = Math.max(120, rect.width - 20);
    const availableH = Math.max(120, rect.height - 20);
    const scale = Math.min(availableW / canvas.width, availableH / canvas.height);
    canvas.style.width = Math.round(canvas.width * scale) + "px";
    canvas.style.height = Math.round(canvas.height * scale) + "px";
  }

  function setFrameSize(width, height) {
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
      fitCanvas();
    }
  }

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
      // A corrupt frame is harmless; the next one replaces it.
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
    document.getElementById("retry").style.display = retryVisible ? "" : "none";
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

    socket.onopen = () => {
      attempts = 0;
      setStatus("warn", "握手中…");
    };

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
          setStatus("online", "已连接 · " + serverWidth + "×" + serverHeight);
          startLatency();
        } else if (message.t === "pong") {
          const rtt = performance.now() - message.ts;
          latencyText.textContent = rtt.toFixed(0) + " ms";
        } else if (message.t === "stats") {
          if (!connected) {
            setStatus("online", "已连接");
          }
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

    socket.onerror = () => {
      setStatus("offline", "连接失败");
    };
  }

  function startLatency() {
    stopLatency();
    latencyTimer = setInterval(() => {
      send({ t: "ping", ts: performance.now() });
    }, 2000);
    send({ t: "ping", ts: performance.now() });
  }

  function stopLatency() {
    clearInterval(latencyTimer);
    latencyTimer = null;
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

  canvas.addEventListener("pointermove", (event) => {
    queueMove(toServer(event));
  });

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

  // ---------------------------------------------------------------- keyboard
  canvas.addEventListener("pointerdown", () => {
    setTimeout(() => kbd.focus({ preventScroll: true }), 0);
  });

  function sendKey(event) {
    send({
      t: "key",
      key: event.key,
      code: event.code,
      ctrl: event.ctrlKey,
      alt: event.altKey,
      shift: event.shiftKey,
      meta: event.metaKey
    });
  }

  kbd.addEventListener("keydown", (event) => {
    if (event.isComposing) return;
    const modifier = event.ctrlKey || event.altKey || event.metaKey;
    if (SPECIAL.has(event.key) || modifier) {
      if (event.key === "Tab" && !modifier) {
        // Tab is meaningful inside the terminal, not for page navigation.
      }
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

  // ---------------------------------------------------------------- toolbar
  qualitySelect.addEventListener("change", () => {
    const value = Number(qualitySelect.value);
    localStorage.setItem("rkdesk.quality", String(value));
    send({ t: "quality", value: value });
  });
  const savedQuality = localStorage.getItem("rkdesk.quality");
  if (savedQuality && qualitySelect.querySelector('option[value="' + savedQuality + '"]')) {
    qualitySelect.value = savedQuality;
  }

  document.getElementById("retry").addEventListener("click", () => {
    attempts = 0;
    connect();
  });

  document.getElementById("restart").addEventListener("click", () => {
    attempts = 0;
    connect();
  });

  document.getElementById("full").addEventListener("click", async () => {
    try {
      if (document.fullscreenElement) {
        await document.exitFullscreen();
      } else {
        await document.documentElement.requestFullscreen();
      }
    } catch (error) { /* ignore */ }
    setTimeout(fitCanvas, 120);
  });

  document.getElementById("paste").addEventListener("click", async () => {
    let text = "";
    try {
      text = await navigator.clipboard.readText();
    } catch (error) {
      text = window.prompt("本机剪贴板不可读，请手动粘贴文本：") || "";
    }
    if (text) send({ t: "paste", text: text });
  });

  document.addEventListener("fullscreenchange", () => setTimeout(fitCanvas, 80));
  window.addEventListener("resize", fitCanvas);
  if (window.ResizeObserver) new ResizeObserver(fitCanvas).observe(stage);

  setInterval(() => {
    const now = performance.now();
    const fps = frameCount * 1000 / Math.max(1, now - lastFpsAt);
    fpsText.textContent = fps.toFixed(0) + " fps";
    frameCount = 0;
    lastFpsAt = now;
    const kbps = byteCount / 1024 / Math.max(0.2, (now - lastBytesAt) / 1000);
    bandwidthText.textContent = kbps.toFixed(0) + " KB/s";
    byteCount = 0;
    lastBytesAt = now;
  }, 1000);

  fitCanvas();
  connect();
})();
