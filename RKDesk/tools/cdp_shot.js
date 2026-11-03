// Capture a real-time screenshot of the running web client through the
// Chrome DevTools Protocol.  Usage:
//   node tools/cdp_shot.js <output.png> [debugPort] [waitMs]
const http = require("http");
const fs = require("fs");
const path = require("path");

let WebSocket;
try {
  WebSocket = require("ws");
} catch (error) {
  WebSocket = require(path.join(__dirname, "../../Visualisation/node_modules/ws"));
}

const output = process.argv[2] || "docs/browser.png";
const port = Number(process.argv[3] || 9223);
const waitMs = Number(process.argv[4] || 6000);

function getJson(url) {
  return new Promise((resolve, reject) => {
    http.get(url, (response) => {
      let body = "";
      response.on("data", (chunk) => (body += chunk));
      response.on("end", () => {
        try { resolve(JSON.parse(body)); } catch (error) { reject(error); }
      });
    }).on("error", reject);
  });
}

(async () => {
  const targets = await getJson(`http://127.0.0.1:${port}/json/list`);
  const page = targets.find((target) => target.type === "page");
  if (!page) throw new Error("no page target");

  const socket = new WebSocket(page.webSocketDebuggerUrl);
  let nextId = 0;
  const pending = new Map();
  socket.on("message", (data) => {
    const message = JSON.parse(data.toString());
    if (message.id && pending.has(message.id)) {
      pending.get(message.id)(message);
      pending.delete(message.id);
    }
  });
  const send = (method, params = {}) => new Promise((resolve) => {
    const id = ++nextId;
    pending.set(id, resolve);
    socket.send(JSON.stringify({ id, method, params }));
  });

  await new Promise((resolve) => socket.on("open", resolve));
  await send("Page.enable");
  await send("Runtime.enable");
  await new Promise((resolve) => setTimeout(resolve, waitMs));
  await send("Runtime.evaluate", { expression: "window.dispatchEvent(new Event('resize'))" });
  await new Promise((resolve) => setTimeout(resolve, 700));

  const state = await send("Runtime.evaluate", {
    expression: `JSON.stringify({
      state: document.getElementById('state').textContent,
      latency: document.getElementById('latency').textContent,
      fps: document.getElementById('fps').textContent,
      bandwidth: document.getElementById('bandwidth').textContent,
      overlay: getComputedStyle(document.getElementById('overlay')).visibility,
      canvas: [document.getElementById('screen').width, document.getElementById('screen').height],
      rect: [Math.round(document.getElementById('screen').getBoundingClientRect().width),
             Math.round(document.getElementById('screen').getBoundingClientRect().height)],
      stage: [document.getElementById('stage').clientWidth, document.getElementById('stage').clientHeight],
      viewport: [innerWidth, innerHeight],
      title: document.title
    })`,
    returnByValue: true
  });
  console.log(state.result.result.value);

  const screenshot = await send("Page.captureScreenshot", { format: "png" });
  fs.mkdirSync(path.dirname(output), { recursive: true });
  fs.writeFileSync(output, Buffer.from(screenshot.result.data, "base64"));
  console.log("saved", output);
  socket.close();
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
