(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const audioBtn = $("audioBtn");
  const micBtn = $("micBtn");
  const audioToggle = $("audioToggle");
  const micToggle = $("micToggle");
  const audioSource = $("audioSource");
  const audioVolume = $("audioVolume");
  const micGain = $("micGain");
  const outMeter = $("outMeter");
  const inMeter = $("inMeter");
  const avBadge = $("avBadge");
  const avNote = $("avNote");
  const micState = $("micState");
  const httpsLink = $("httpsLink");
  const testTone = $("testTone");

  const state = {
    ws: null,
    ctx: null,
    gain: null,
    playing: false,
    micOn: false,
    micStream: null,
    micSource: null,
    micNode: null,
    micSink: null,
    nextTime: 0,
    outLevel: 0,
    inLevel: 0,
    micGain: 1,
    status: null,
    reconnectTimer: null
  };

  function wsUrl() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const token = new URLSearchParams(location.search).get("token");
    return proto + "//" + location.host + "/av" + (token ? "?token=" + encodeURIComponent(token) : "");
  }

  function send(message) {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify(message));
      return true;
    }
    return false;
  }

  function connect() {
    clearTimeout(state.reconnectTimer);
    if (state.ws) {
      try { state.ws.close(); } catch (error) { /* ignore */ }
    }
    state.ws = new WebSocket(wsUrl());
    state.ws.binaryType = "arraybuffer";
    state.ws.onopen = () => {
      if (state.playing) send({ t: "audio_start", source: audioSource.value });
      if (state.micOn) send({ t: "mic_start" });
    };
    state.ws.onmessage = (event) => {
      if (typeof event.data === "string") {
        let message;
        try { message = JSON.parse(event.data); } catch (error) { return; }
        if (message.t === "av") updateStatus(message);
        return;
      }
      if (state.playing) playPcm(event.data);
    };
    state.ws.onclose = () => {
      setBadge("offline", "音频断开");
      state.reconnectTimer = setTimeout(connect, 2000);
    };
    state.ws.onerror = () => setBadge("bad", "音频错误");
  }

  function setBadge(kind, text) {
    avBadge.textContent = text;
    avBadge.className = "av-badge" + (kind === "ok" ? " ok" : kind === "bad" ? " bad" : "");
  }

  function updateStatus(status) {
    state.status = status;
    setBadge(status.pulse ? "ok" : "bad", status.pulse ? "PulseAudio" : "ALSA");
    avNote.textContent = status.note || (status.pulse ? "PulseAudio 已就绪" : "仅板端播放，不支持虚拟麦克风");

    const current = audioSource.value;
    const sources = status.sources || [];
    const signature = sources.map((item) => item.name + item.label).join("|");
    if (audioSource.dataset.signature !== signature) {
      audioSource.innerHTML = "";
      sources.forEach((item) => {
        const option = document.createElement("option");
        option.value = item.name;
        option.textContent = item.label;
        audioSource.appendChild(option);
      });
      audioSource.dataset.signature = signature;
    }
    const wanted = current || status.audio_source || (sources[0] && sources[0].name);
    if (wanted && sources.some((item) => item.name === wanted)) {
      audioSource.value = wanted;
    }

    micToggle.disabled = !status.virtual_mic_supported;
    if (!status.virtual_mic_supported) {
      micState.textContent = "不可用";
    } else if (!state.micOn) {
      micState.textContent = status.virtual_mic ? "板端已启用" : "未启用";
    }

    httpsLink.hidden = window.isSecureContext;
    if (!window.isSecureContext) {
      const port = status.https_port || 8443;
      httpsLink.href = "https://" + location.hostname + ":" + port + location.pathname + location.search;
      avNote.textContent = "麦克风需要 HTTPS：先切换到安全页面并信任自签名证书。";
    }
    if (status.last_error) {
      avNote.textContent = status.last_error;
      if (state.micOn) stopMic(false);
    }
  }

  async function ensureContext() {
    if (!state.ctx) {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      state.ctx = new Ctx({ sampleRate: 48000 });
      state.gain = state.ctx.createGain();
      state.gain.gain.value = Number(audioVolume.value) / 100;
      state.gain.connect(state.ctx.destination);
    }
    if (state.ctx.state === "suspended") await state.ctx.resume();
    return state.ctx;
  }

  async function startAudio() {
    try {
      await ensureContext();
    } catch (error) {
      avNote.textContent = "浏览器无法创建 AudioContext：" + error.message;
      return;
    }
    state.playing = true;
    state.nextTime = 0;
    audioToggle.classList.add("active");
    audioBtn.classList.add("active");
    audioToggle.textContent = "停止收听";
    send({ t: "audio_start", source: audioSource.value });
  }

  function stopAudio() {
    state.playing = false;
    state.outLevel = 0;
    audioToggle.classList.remove("active");
    audioBtn.classList.remove("active");
    audioToggle.textContent = "听板端声音";
    send({ t: "audio_stop" });
  }

  function playPcm(buffer) {
    if (!state.ctx || !state.playing) return;
    const samples = new Int16Array(buffer);
    const frames = Math.floor(samples.length / 2);
    if (!frames) return;
    const audioBuffer = state.ctx.createBuffer(2, frames, 48000);
    const left = audioBuffer.getChannelData(0);
    const right = audioBuffer.getChannelData(1);
    let sum = 0;
    for (let i = 0; i < frames; i += 1) {
      const l = samples[i * 2] / 32768;
      const r = samples[i * 2 + 1] / 32768;
      left[i] = l;
      right[i] = r;
      sum += l * l + r * r;
    }
    const rms = Math.sqrt(sum / Math.max(1, frames * 2));
    state.outLevel = Math.max(rms * 2.5, state.outLevel * 0.75);

    const source = state.ctx.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(state.gain);
    const now = state.ctx.currentTime;
    if (state.nextTime < now + 0.03 || state.nextTime > now + 0.8) {
      state.nextTime = now + 0.08;
    }
    source.start(state.nextTime);
    state.nextTime += audioBuffer.duration;
  }

  async function installMicWorklet(context) {
    const code = `
      class RkdeskMic extends AudioWorkletProcessor {
        constructor() {
          super();
          this.buffer = new Float32Array(960);
          this.offset = 0;
        }
        process(inputs) {
          const channel = inputs[0] && inputs[0][0];
          if (!channel) return true;
          for (let i = 0; i < channel.length; i += 1) {
            this.buffer[this.offset++] = channel[i];
            if (this.offset >= this.buffer.length) {
              this.port.postMessage(this.buffer.slice(0));
              this.offset = 0;
            }
          }
          return true;
        }
      }
      registerProcessor("rkdesk-mic", RkdeskMic);
    `;
    const url = URL.createObjectURL(new Blob([code], { type: "application/javascript" }));
    try {
      await context.audioWorklet.addModule(url);
    } finally {
      URL.revokeObjectURL(url);
    }
  }

  async function startMic() {
    if (!window.isSecureContext) {
      avNote.textContent = "麦克风需要 HTTPS。请点击下方链接切换到安全页面。";
      httpsLink.hidden = false;
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      avNote.textContent = "当前浏览器不支持 getUserMedia。";
      return;
    }
    try {
      state.micStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true
        }
      });
      const context = await ensureContext();
      await installMicWorklet(context);
      state.micSource = context.createMediaStreamSource(state.micStream);
      state.micNode = new AudioWorkletNode(context, "rkdesk-mic");
      state.micSink = context.createGain();
      state.micSink.gain.value = 0;
      state.micSource.connect(state.micNode);
      state.micNode.connect(state.micSink);
      state.micSink.connect(context.destination);

      state.micNode.port.onmessage = (event) => {
        if (!state.micOn) return;
        const frame = event.data;
        const pcm = new Int16Array(frame.length);
        let sum = 0;
        for (let i = 0; i < frame.length; i += 1) {
          const value = Math.max(-1, Math.min(1, frame[i] * state.micGain));
          pcm[i] = value * 32767;
          sum += value * value;
        }
        state.inLevel = Math.max(Math.sqrt(sum / frame.length) * 3, state.inLevel * 0.7);
        if (state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.send(pcm.buffer);
      };

      state.micOn = true;
      micToggle.classList.add("active");
      micBtn.classList.add("active");
      micToggle.textContent = "停止麦克风";
      micState.textContent = "发送中";
      send({ t: "mic_start" });
    } catch (error) {
      avNote.textContent = "无法打开麦克风：" + error.message;
      stopMic(false);
    }
  }

  function stopMic(notify = true) {
    state.micOn = false;
    micToggle.classList.remove("active");
    micBtn.classList.remove("active");
    micToggle.textContent = "发送麦克风";
    micState.textContent = "未启用";
    state.inLevel = 0;
    if (state.micNode) {
      try { state.micNode.disconnect(); } catch (error) { /* ignore */ }
      state.micNode = null;
    }
    if (state.micSource) {
      try { state.micSource.disconnect(); } catch (error) { /* ignore */ }
      state.micSource = null;
    }
    if (state.micSink) {
      try { state.micSink.disconnect(); } catch (error) { /* ignore */ }
      state.micSink = null;
    }
    if (state.micStream) {
      state.micStream.getTracks().forEach((track) => track.stop());
      state.micStream = null;
    }
    if (notify) send({ t: "mic_stop" });
  }

  audioToggle.addEventListener("click", () => {
    if (state.playing) stopAudio(); else startAudio();
  });
  audioBtn.addEventListener("click", () => {
    if (state.playing) stopAudio(); else startAudio();
  });
  micToggle.addEventListener("click", () => {
    if (state.micOn) stopMic(); else startMic();
  });
  micBtn.addEventListener("click", () => {
    if (state.micOn) stopMic(); else startMic();
  });
  audioSource.addEventListener("change", () => {
    if (state.playing) send({ t: "audio_start", source: audioSource.value });
  });
  audioVolume.addEventListener("input", () => {
    if (state.gain) state.gain.gain.value = Number(audioVolume.value) / 100;
  });
  micGain.addEventListener("input", () => {
    state.micGain = Number(micGain.value) / 100;
  });
  testTone.addEventListener("click", () => send({ t: "test_tone" }));

  function animateMeters() {
    outMeter.style.width = Math.min(100, state.outLevel * 100) + "%";
    inMeter.style.width = Math.min(100, state.inLevel * 100) + "%";
    state.outLevel *= 0.9;
    state.inLevel *= 0.9;
    requestAnimationFrame(animateMeters);
  }

  window.addEventListener("beforeunload", () => {
    if (state.micOn) stopMic(false);
  });

  if (!window.isSecureContext) {
    httpsLink.hidden = false;
    const port = 8443;
    httpsLink.href = "https://" + location.hostname + ":" + port + location.pathname + location.search;
    avNote.textContent = "麦克风需要 HTTPS：先切换到安全页面并信任自签名证书。";
  }

  animateMeters();
  connect();
})();
