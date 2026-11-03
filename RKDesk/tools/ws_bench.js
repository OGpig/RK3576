// Measure frame rate and bandwidth while moving the pointer continuously.
// Usage: node tools/ws_bench.js [wsUrl] [seconds]
const path = require("path");
let WebSocket;
try {
  WebSocket = require("ws");
} catch (error) {
  WebSocket = require(path.join(__dirname, "../node_modules/ws"));
}

const url = process.argv[2] || "ws://192.168.31.153:8088/ws";
const seconds = Number(process.argv[3] || 6);
const socket = new WebSocket(url);
let frames = 0;
let bytes = 0;
let started = 0;
let timer = null;

socket.on("open", () => {
  console.log("open", url);
  socket.on("message", (data, binary) => {
    if (!binary) {
      return;
    }
    frames += 1;
    bytes += data.length;
  });
  timer = setInterval(() => {
    if (!started) return;
    const t = (Date.now() - started) / 1000;
    const x = 400 + Math.round(220 * Math.cos(t * 2.2));
    const y = 300 + Math.round(160 * Math.sin(t * 2.2));
    socket.send(JSON.stringify({ t: "move", x, y }));
  }, 16);
  started = Date.now();
  setTimeout(() => {
    clearInterval(timer);
    const elapsed = (Date.now() - started) / 1000;
    console.log(JSON.stringify({
      seconds: elapsed,
      frames,
      fps: +(frames / elapsed).toFixed(1),
      kbps: +(bytes / 1024 / elapsed).toFixed(0),
      avgFrameKB: +(bytes / 1024 / Math.max(1, frames)).toFixed(1)
    }, null, 2));
    socket.close();
  }, seconds * 1000);
});

socket.on("error", (error) => {
  console.error("error", error.message);
  process.exit(1);
});
