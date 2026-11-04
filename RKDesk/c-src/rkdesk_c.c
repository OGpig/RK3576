/*
 * RKDesk C mirror - a performance-oriented screen mirror for RK3576.
 *
 * It captures the active DRM CRTC through a dma-buf, rotates the 800x1280
 * panel buffer into a 1280x800 desktop, encodes with libjpeg-turbo and serves
 * JPEG frames over a tiny WebSocket server on port 8089.  Input events are
 * forwarded to the existing rkdesk input daemon, so this binary does not need
 * uinput code of its own.
 *
 * Build (Windows host, Zig):
 *   zig cc -target aarch64-linux-gnu -O3 -pthread \
 *       -L ../toolchains/board-libs -l:libturbojpeg.so.0 rkdesk_c.c -o rkdesk_c
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include <drm/drm.h>
#include <drm/drm_mode.h>

/* --- minimal TurboJPEG ABI (runtime lib is present on the board) --------- */
typedef void *tjhandle;
#define TJPF_BGRX 3
#define TJSAMP_422 1
#define TJSAMP_420 2
#define TJFLAG_FASTDCT 2048
extern tjhandle tjInitCompress(void);
extern int tjCompress2(tjhandle, const unsigned char *, int, int, int, int,
                       unsigned char **, unsigned long *, int, int, int);
extern int tjDestroy(tjhandle);
extern char *tjGetErrorStr(void);

#define INPUT_SOCKET "/var/run/rkdesk-input.sock"
#define MAX_CLIENTS 16

static volatile int g_running = 1;
static int g_port = 8089;
static int g_fps_target = 30;
static int g_quality = 80;
static int g_subsamp = TJSAMP_420;
static const char *g_device = "/dev/dri/card0";

static int g_drm_fd = -1;
static uint32_t g_crtc_id = 0;
static int g_raw_w = 0, g_raw_h = 0, g_pitch = 0;
static int g_disp_w = 0, g_disp_h = 0;
static int g_rotation = 0;
static void *g_map = NULL;
static size_t g_map_size = 0;
static pthread_mutex_t g_frame_lock = PTHREAD_MUTEX_INITIALIZER;
static unsigned char *g_latest = NULL;
static size_t g_latest_size = 0;
static double g_encode_ms = 0;
static double g_fps = 0;

/* --- clients ------------------------------------------------------------- */
typedef struct Client {
    int fd;
    pthread_mutex_t lock;
    struct Client *next;
} Client;

static Client *g_clients = NULL;
static pthread_mutex_t g_clients_lock = PTHREAD_MUTEX_INITIALIZER;

/* --- input relay --------------------------------------------------------- */
static int g_input_fd = -1;
static pthread_mutex_t g_input_lock = PTHREAD_MUTEX_INITIALIZER;

/* --- tiny SHA1/base64 for the WebSocket handshake ----------------------- */
typedef struct {
    uint32_t h[5];
    uint64_t len;
    unsigned char buf[64];
    size_t used;
} Sha1;

static uint32_t rol(uint32_t v, int n) { return (v << n) | (v >> (32 - n)); }

static void sha1_block(Sha1 *s, const unsigned char *p) {
    uint32_t w[80], a, b, c, d, e, t;
    for (int i = 0; i < 16; i++)
        w[i] = ((uint32_t)p[i * 4] << 24) | ((uint32_t)p[i * 4 + 1] << 16) |
               ((uint32_t)p[i * 4 + 2] << 8) | p[i * 4 + 3];
    for (int i = 16; i < 80; i++) w[i] = rol(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1);
    a = s->h[0]; b = s->h[1]; c = s->h[2]; d = s->h[3]; e = s->h[4];
    for (int i = 0; i < 80; i++) {
        if (i < 20) t = ((b & c) | ((~b) & d)) + 0x5A827999;
        else if (i < 40) t = (b ^ c ^ d) + 0x6ED9EBA1;
        else if (i < 60) t = ((b & c) | (b & d) | (c & d)) + 0x8F1BBCDC;
        else t = (b ^ c ^ d) + 0xCA62C1D6;
        t += rol(a, 5) + e + w[i];
        e = d; d = c; c = rol(b, 30); b = a; a = t;
    }
    s->h[0] += a; s->h[1] += b; s->h[2] += c; s->h[3] += d; s->h[4] += e;
}

static void sha1_init(Sha1 *s) {
    s->h[0] = 0x67452301; s->h[1] = 0xEFCDAB89; s->h[2] = 0x98BADCFE;
    s->h[3] = 0x10325476; s->h[4] = 0xC3D2E1F0;
    s->len = 0; s->used = 0;
}

static void sha1_update(Sha1 *s, const void *data, size_t len) {
    const unsigned char *p = data;
    s->len += len;
    while (len) {
        size_t n = 64 - s->used;
        if (n > len) n = len;
        memcpy(s->buf + s->used, p, n);
        s->used += n; p += n; len -= n;
        if (s->used == 64) { sha1_block(s, s->buf); s->used = 0; }
    }
}

static void sha1_final(Sha1 *s, unsigned char out[20]) {
    uint64_t bits = s->len * 8;
    unsigned char pad = 0x80;
    sha1_update(s, &pad, 1);
    unsigned char zero = 0;
    while (s->used != 56) sha1_update(s, &zero, 1);
    unsigned char lenbuf[8];
    for (int i = 0; i < 8; i++) lenbuf[i] = (unsigned char)(bits >> (56 - i * 8));
    sha1_update(s, lenbuf, 8);
    for (int i = 0; i < 5; i++) {
        out[i * 4] = s->h[i] >> 24; out[i * 4 + 1] = s->h[i] >> 16;
        out[i * 4 + 2] = s->h[i] >> 8; out[i * 4 + 3] = s->h[i];
    }
}

static const char B64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
static void base64(const unsigned char *in, size_t len, char *out) {
    size_t i = 0, o = 0;
    while (i + 2 < len) {
        uint32_t v = ((uint32_t)in[i] << 16) | ((uint32_t)in[i + 1] << 8) | in[i + 2];
        out[o++] = B64[(v >> 18) & 63]; out[o++] = B64[(v >> 12) & 63];
        out[o++] = B64[(v >> 6) & 63]; out[o++] = B64[v & 63];
        i += 3;
    }
    if (i < len) {
        uint32_t v = (uint32_t)in[i] << 16;
        if (i + 1 < len) v |= (uint32_t)in[i + 1] << 8;
        out[o++] = B64[(v >> 18) & 63]; out[o++] = B64[(v >> 12) & 63];
        out[o++] = (i + 1 < len) ? B64[(v >> 6) & 63] : '=';
        out[o++] = '=';
    }
    out[o] = 0;
}

/* --- time ---------------------------------------------------------------- */
static double now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1000000.0;
}

/* --- DRM capture --------------------------------------------------------- */
static int drm_init(void) {
    g_drm_fd = open(g_device, O_RDWR);
    if (g_drm_fd < 0) { perror("open drm"); return -1; }

    struct drm_mode_card_res res;
    memset(&res, 0, sizeof(res));
    if (ioctl(g_drm_fd, DRM_IOCTL_MODE_GETRESOURCES, &res) < 0) { perror("getresources"); return -1; }
    uint32_t *crtcs = calloc(res.count_crtcs, sizeof(uint32_t));
    uint32_t *fbs = calloc(res.count_fbs, sizeof(uint32_t));
    uint32_t *conns = calloc(res.count_connectors, sizeof(uint32_t));
    uint32_t *encs = calloc(res.count_encoders, sizeof(uint32_t));
    res.crtc_id_ptr = (uint64_t)(uintptr_t)crtcs;
    res.fb_id_ptr = (uint64_t)(uintptr_t)fbs;
    res.connector_id_ptr = (uint64_t)(uintptr_t)conns;
    res.encoder_id_ptr = (uint64_t)(uintptr_t)encs;
    if (ioctl(g_drm_fd, DRM_IOCTL_MODE_GETRESOURCES, &res) < 0) { perror("getresources2"); return -1; }

    uint64_t best = 0;
    for (uint32_t i = 0; i < res.count_crtcs; i++) {
        struct drm_mode_crtc crtc;
        memset(&crtc, 0, sizeof(crtc));
        crtc.crtc_id = crtcs[i];
        if (ioctl(g_drm_fd, DRM_IOCTL_MODE_GETCRTC, &crtc) < 0) continue;
        uint64_t area = (uint64_t)crtc.mode.hdisplay * crtc.mode.vdisplay;
        if (crtc.mode_valid && crtc.fb_id && area > best) {
            best = area;
            g_crtc_id = crtc.crtc_id;
        }
    }
    free(crtcs); free(fbs); free(conns); free(encs);
    if (!g_crtc_id) { fprintf(stderr, "no active CRTC\n"); return -1; }

    struct drm_mode_crtc crtc;
    memset(&crtc, 0, sizeof(crtc));
    crtc.crtc_id = g_crtc_id;
    if (ioctl(g_drm_fd, DRM_IOCTL_MODE_GETCRTC, &crtc) < 0) { perror("getcrtc"); return -1; }

    struct drm_mode_fb_cmd fb;
    memset(&fb, 0, sizeof(fb));
    fb.fb_id = crtc.fb_id;
    if (ioctl(g_drm_fd, DRM_IOCTL_MODE_GETFB, &fb) < 0) { perror("getfb"); return -1; }

    struct drm_prime_handle prime;
    memset(&prime, 0, sizeof(prime));
    prime.handle = fb.handle;
    prime.flags = DRM_CLOEXEC | DRM_RDWR;
    if (ioctl(g_drm_fd, DRM_IOCTL_PRIME_HANDLE_TO_FD, &prime) < 0) { perror("prime"); return -1; }

    g_map_size = (size_t)fb.pitch * fb.height;
    g_map = mmap(NULL, g_map_size, PROT_READ, MAP_SHARED, prime.fd, 0);
    close(prime.fd);
    if (g_map == MAP_FAILED) { perror("mmap"); return -1; }

    g_raw_w = fb.width;
    g_raw_h = fb.height;
    g_pitch = fb.pitch;
    if (g_raw_w > g_raw_h) {
        g_rotation = 0;
        g_disp_w = g_raw_w;
        g_disp_h = g_raw_h;
    } else {
        g_rotation = 90;
        g_disp_w = g_raw_h;
        g_disp_h = g_raw_w;
    }
    printf("drm %dx%d pitch=%d display=%dx%d rot=%d\n",
           g_raw_w, g_raw_h, g_pitch, g_disp_w, g_disp_h, g_rotation);
    return 0;
}

/* --- input relay --------------------------------------------------------- */
static int input_connect(void) {
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) return -1;
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, INPUT_SOCKET, sizeof(addr.sun_path) - 1);
    if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) { close(fd); return -1; }
    char resize[96];
    int n = snprintf(resize, sizeof(resize), "{\"t\":\"resize\",\"w\":%d,\"h\":%d}\n", g_disp_w, g_disp_h);
    send(fd, resize, n, MSG_NOSIGNAL);
    return fd;
}

static void input_forward(const char *data, size_t len) {
    pthread_mutex_lock(&g_input_lock);
    if (g_input_fd < 0) g_input_fd = input_connect();
    if (g_input_fd >= 0) {
        if (send(g_input_fd, data, len, MSG_NOSIGNAL) < 0 ||
            send(g_input_fd, "\n", 1, MSG_NOSIGNAL) < 0) {
            close(g_input_fd);
            g_input_fd = input_connect();
            if (g_input_fd >= 0) send(g_input_fd, data, len, MSG_NOSIGNAL);
            if (g_input_fd >= 0) send(g_input_fd, "\n", 1, MSG_NOSIGNAL);
        }
    }
    pthread_mutex_unlock(&g_input_lock);
}

/* --- client queue -------------------------------------------------------- */
static void client_add(Client *c) {
    pthread_mutex_lock(&g_clients_lock);
    c->next = g_clients;
    g_clients = c;
    pthread_mutex_unlock(&g_clients_lock);
}

static void client_remove(Client *c) {
    pthread_mutex_lock(&g_clients_lock);
    Client **p = &g_clients;
    while (*p && *p != c) p = &(*p)->next;
    if (*p) *p = c->next;
    pthread_mutex_unlock(&g_clients_lock);
    pthread_mutex_lock(&c->lock);
    if (c->fd >= 0) close(c->fd);
    c->fd = -1;
    pthread_mutex_unlock(&c->lock);
    pthread_mutex_destroy(&c->lock);
    free(c);
}

static int send_all(int fd, const void *data, size_t len) {
    const unsigned char *p = data;
    while (len) {
        ssize_t n = send(fd, p, len, MSG_NOSIGNAL);
        if (n <= 0) return -1;
        p += n; len -= (size_t)n;
    }
    return 0;
}

static int ws_send_binary(Client *c, const unsigned char *data, size_t len) {
    unsigned char head[10];
    size_t hlen = 0;
    head[hlen++] = 0x82;
    if (len < 126) head[hlen++] = (unsigned char)len;
    else if (len < 65536) {
        head[hlen++] = 126;
        head[hlen++] = (unsigned char)(len >> 8);
        head[hlen++] = (unsigned char)len;
    } else {
        head[hlen++] = 127;
        for (int i = 7; i >= 0; i--) head[hlen++] = (unsigned char)(len >> (i * 8));
    }
    pthread_mutex_lock(&c->lock);
    int rc = (c->fd >= 0) ? (send_all(c->fd, head, hlen) || send_all(c->fd, data, len)) : -1;
    pthread_mutex_unlock(&c->lock);
    return rc;
}

/* --- capture thread ------------------------------------------------------ */
static tjhandle g_tj = NULL;

static int encode_frame(unsigned char **jpeg, unsigned long *size) {
    /* Encode native scanout; the browser canvas rotates it on the GPU. */
    *jpeg = NULL;
    *size = 0;
    int rc = tjCompress2(g_tj, (const unsigned char *)g_map, g_raw_w, g_pitch,
                         g_raw_h, TJPF_BGRX, jpeg, size, g_subsamp,
                         g_quality, TJFLAG_FASTDCT);
    if (rc != 0) {
        fprintf(stderr, "tjCompress2: %s\n", tjGetErrorStr());
        return -1;
    }
    return 0;
}

static void *capture_loop(void *arg) {
    (void)arg;
    g_tj = tjInitCompress();
    if (!g_tj) { fprintf(stderr, "tjInitCompress: %s\n", tjGetErrorStr()); return NULL; }
    double frames = 0, last = now_ms(), fps_at = last;
    while (g_running) {
        pthread_mutex_lock(&g_clients_lock);
        int has_clients = g_clients != NULL;
        pthread_mutex_unlock(&g_clients_lock);
        if (!has_clients) {
            g_fps = 0;
            usleep(100000);
            continue;
        }
        double t0 = now_ms();
        unsigned char *jpeg = NULL;
        unsigned long size = 0;
        if (encode_frame(&jpeg, &size) == 0) {
            g_encode_ms = now_ms() - t0;
            pthread_mutex_lock(&g_frame_lock);
            free(g_latest);
            g_latest = malloc(size);
            if (g_latest) { memcpy(g_latest, jpeg, size); g_latest_size = size; }
            pthread_mutex_unlock(&g_frame_lock);

            pthread_mutex_lock(&g_clients_lock);
            for (Client *c = g_clients; c; c = c->next) {
                ws_send_binary(c, jpeg, size);
            }
            pthread_mutex_unlock(&g_clients_lock);
            free(jpeg);
            frames++;
        }
        double now = now_ms();
        if (now - fps_at >= 1000.0) {
            g_fps = frames * 1000.0 / (now - fps_at);
            frames = 0;
            fps_at = now;
        }
        double spent = now_ms() - t0;
        double wait = 1000.0 / g_fps_target - spent;
        if (wait > 1) usleep((useconds_t)(wait * 1000));
    }
    tjDestroy(g_tj);
    return NULL;
}

/* --- HTTP / WebSocket ---------------------------------------------------- */
static const char *PAGE =
"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
"<title>RKDesk C mirror</title><style>"
"*{box-sizing:border-box}html,body{margin:0;height:100%;background:#05080b;color:#e2edf5;font:14px 'Segoe UI',sans-serif;overflow:hidden}"
"header{height:48px;display:flex;align-items:center;gap:18px;padding:0 18px;background:#0b141b;border-bottom:1px solid #1d2b37}"
"b{color:#37e0b0}#hud{margin-left:auto;font:11px monospace;color:#8397a8}#hud span{margin-left:14px}"
"main{height:calc(100% - 48px);display:grid;place-items:center;padding:14px}"
"canvas{max-width:100%;max-height:100%;border-radius:10px;box-shadow:0 20px 60px #000a;cursor:none;touch-action:none;background:#000}"
"#k{position:fixed;left:-1000px;top:0;opacity:0}"
"</style></head><body><header><b>RK3576 C mirror</b><span>DRM + TurboJPEG · port 8089</span>"
"<div id=hud><span id=stat>connecting</span><span id=info>--</span></div></header>"
"<main><canvas id=c width=1280 height=800 tabindex=0></canvas></main><textarea id=k></textarea><script>"
"const c=document.getElementById('c'),ctx=c.getContext('2d',{alpha:false}),k=document.getElementById('k'),stat=document.getElementById('stat'),info=document.getElementById('info');"
"let ws,W=1280,H=800,rot=90,frames=0,t0=performance.now(),fps=0,next=0;"
"function fit(){let m=document.querySelector('main'),s=Math.min((m.clientWidth-28)/W,(m.clientHeight-28)/H);c.style.width=Math.round(W*s)+'px';c.style.height=Math.round(H*s)+'px';}"
"addEventListener('resize',fit);"
"function dev(p){if(rot===90)return{x:Math.round(p.y*W/H),y:Math.round((1-p.x/W)*H)};return{x:Math.round(p.x),y:Math.round(p.y)};}"
"function pos(e){let r=c.getBoundingClientRect(),x=(e.clientX-r.left)/r.width*W,y=(e.clientY-r.top)/r.height*H;return{x:Math.max(0,Math.min(W-1,x)),y:Math.max(0,Math.min(H-1,y))};}"
"function sendMove(p){let d=dev(p);ws.send(JSON.stringify({t:'move',x:d.x,y:d.y}));return d;}"
"function connect(){ws=new WebSocket((location.protocol==='https:'?'wss':'ws')+'://'+location.host+'/ws');ws.binaryType='arraybuffer';"
"ws.onopen=()=>{stat.textContent='connected';};ws.onclose=()=>{stat.textContent='closed';setTimeout(connect,1500);};"
"ws.onmessage=async e=>{if(typeof e.data==='string')return;let b=new Blob([e.data],{type:'image/jpeg'});try{let im=await createImageBitmap(b);if(rot===90){ctx.save();ctx.translate(W,0);ctx.rotate(Math.PI/2);ctx.drawImage(im,0,0);ctx.restore();}else{if(im.width!==W||im.height!==H){W=im.width;H=im.height;c.width=W;c.height=H;fit();}ctx.drawImage(im,0,0);}im.close();frames++;}catch(err){}};}"
"c.addEventListener('pointerdown',e=>{e.preventDefault();c.setPointerCapture(e.pointerId);k.focus();let d=sendMove(pos(e));setTimeout(()=>ws.send(JSON.stringify({t:'button',b:e.button,down:true})),25);});"
"c.addEventListener('pointermove',e=>{if(ws&&ws.readyState===1)sendMove(pos(e));});"
"c.addEventListener('pointerup',e=>{e.preventDefault();let d=dev(pos(e));setTimeout(()=>ws.send(JSON.stringify({t:'button',b:e.button,down:false})),60);});"
"c.addEventListener('wheel',e=>{e.preventDefault();let d=sendMove(pos(e));ws.send(JSON.stringify({t:'wheel',x:d.x,y:d.y,dy:Math.sign(e.deltaY)}));},{passive:false});"
"c.addEventListener('contextmenu',e=>e.preventDefault());"
"k.addEventListener('keydown',e=>{let sp=['Enter','Backspace','Tab','Escape','ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Home','End','Delete','PageUp','PageDown','Insert','F1','F2','F3','F4','F5','F6','F7','F8','F9','F10','F11','F12'];if(sp.includes(e.key)||e.ctrlKey||e.altKey||e.metaKey){ws.send(JSON.stringify({t:'key',key:e.key,code:e.code,ctrl:e.ctrlKey,alt:e.altKey,shift:e.shiftKey,meta:e.metaKey}));e.preventDefault();}});"
"k.addEventListener('input',()=>{if(k.value){ws.send(JSON.stringify({t:'text',text:k.value}));k.value='';}});"
"setInterval(async()=>{try{let r=await fetch('/healthz').then(x=>x.json());rot=r.rotation;if(r.width!==W||r.height!==H){W=r.width;H=r.height;c.width=W;c.height=H;fit();}let now=performance.now();fps=frames*1000/(now-t0);frames=0;t0=now;info.textContent=r.fps.toFixed(1)+' fps · '+r.encode_ms.toFixed(1)+' ms enc · '+W+'x'+H;}catch(e){}},1000);"
"fit();connect();</script></body></html>";

static void http_response(int fd, int code, const char *type, const void *body, size_t len) {
    char head[256];
    const char *reason = code == 200 ? "OK" : code == 404 ? "Not Found" : "Error";
    int n = snprintf(head, sizeof(head),
                     "HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %zu\r\n"
                     "Cache-Control: no-store\r\nConnection: close\r\n\r\n",
                     code, reason, type, len);
    send_all(fd, head, n);
    if (len) send_all(fd, body, len);
}

static char *find_header(char *req, const char *name) {
    size_t n = strlen(name);
    char *p = req;
    while ((p = strcasestr(p, name))) {
        if (p == req || p[-1] == '\n') {
            p += n;
            while (*p == ' ' || *p == '\t') p++;
            return p;
        }
        p += n;
    }
    return NULL;
}

static void ws_handshake(int fd, char *key) {
    Sha1 s;
    unsigned char digest[20];
    char concat[256], accept[64];
    snprintf(concat, sizeof(concat), "%s258EAFA5-E914-47DA-95CA-C5AB0DC85B11", key);
    sha1_init(&s);
    sha1_update(&s, concat, strlen(concat));
    sha1_final(&s, digest);
    base64(digest, 20, accept);
    char head[256];
    int n = snprintf(head, sizeof(head),
                     "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                     "Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n", accept);
    send_all(fd, head, n);
}

static int ws_read_frame(int fd, unsigned char *opcode, unsigned char **payload, uint64_t *plen) {
    unsigned char h[2];
    if (recv(fd, h, 2, MSG_WAITALL) != 2) return -1;
    *opcode = h[0] & 0x0F;
    int masked = h[1] & 0x80;
    uint64_t len = h[1] & 0x7F;
    if (len == 126) {
        unsigned char b[2];
        if (recv(fd, b, 2, MSG_WAITALL) != 2) return -1;
        len = ((uint64_t)b[0] << 8) | b[1];
    } else if (len == 127) {
        unsigned char b[8];
        if (recv(fd, b, 8, MSG_WAITALL) != 8) return -1;
        len = 0;
        for (int i = 0; i < 8; i++) len = (len << 8) | b[i];
    }
    if (len > 65536) return -1;
    unsigned char mask[4] = {0, 0, 0, 0};
    if (masked && recv(fd, mask, 4, MSG_WAITALL) != 4) return -1;
    unsigned char *buf = malloc(len + 1);
    if (!buf) return -1;
    if (len && recv(fd, buf, len, MSG_WAITALL) != (ssize_t)len) { free(buf); return -1; }
    if (masked) for (uint64_t i = 0; i < len; i++) buf[i] ^= mask[i % 4];
    buf[len] = 0;
    *payload = buf;
    *plen = len;
    return 0;
}

typedef struct { int fd; } ConnArg;

static void *conn_thread(void *arg) {
    int fd = ((ConnArg *)arg)->fd;
    free(arg);
    char req[8192];
    size_t used = 0;
    while (used < sizeof(req) - 1) {
        ssize_t n = recv(fd, req + used, sizeof(req) - 1 - used, 0);
        if (n <= 0) { close(fd); return NULL; }
        used += (size_t)n;
        if (strstr(req, "\r\n\r\n")) break;
    }
    req[used] = 0;
    char method[8], path[256];
    if (sscanf(req, "%7s %255s", method, path) != 2) { close(fd); return NULL; }

    if (strcmp(path, "/healthz") == 0) {
        char body[256];
        int n = snprintf(body, sizeof(body),
                         "{\"ok\":true,\"c\":true,\"width\":%d,\"height\":%d,\"rotation\":%d,"
                         "\"fps\":%.1f,\"encode_ms\":%.1f}\n",
                         g_disp_w, g_disp_h, g_rotation, g_fps, g_encode_ms);
        http_response(fd, 200, "application/json", body, n);
        close(fd); return NULL;
    }
    if (strcmp(path, "/api/screenshot.jpg") == 0) {
        pthread_mutex_lock(&g_frame_lock);
        size_t len = g_latest_size;
        unsigned char *copy = len ? malloc(len) : NULL;
        if (copy) memcpy(copy, g_latest, len);
        pthread_mutex_unlock(&g_frame_lock);
        if (!copy) { http_response(fd, 503, "text/plain", "no frame", 8); close(fd); return NULL; }
        http_response(fd, 200, "image/jpeg", copy, len);
        free(copy); close(fd); return NULL;
    }
    if (strcmp(path, "/ws") == 0) {
        char *key = find_header(req, "Sec-WebSocket-Key:");
        char *up = find_header(req, "Upgrade:");
        if (!key || !up || !strcasestr(up, "websocket")) {
            http_response(fd, 400, "text/plain", "websocket required", 18);
            close(fd); return NULL;
        }
        char *end = key;
        while (*end && *end != '\r' && *end != '\n') end++;
        *end = 0;
        ws_handshake(fd, key);
        Client *c = calloc(1, sizeof(Client));
        c->fd = fd;
        pthread_mutex_init(&c->lock, NULL);
        struct timeval tv = {1, 0};
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
        client_add(c);
        while (g_running) {
            unsigned char opcode;
            unsigned char *payload = NULL;
            uint64_t len = 0;
            if (ws_read_frame(fd, &opcode, &payload, &len) < 0) break;
            if (opcode == 0x8) { free(payload); break; }
            if (opcode == 0x9) {
                unsigned char pong[2] = {0x8A, 0};
                send_all(fd, pong, 2);
            } else if (opcode == 0x1) {
                input_forward((const char *)payload, len);
            }
            free(payload);
        }
        client_remove(c);
        return NULL;
    }
    http_response(fd, 200, "text/html; charset=utf-8", PAGE, strlen(PAGE));
    close(fd);
    return NULL;
}

int main(int argc, char **argv) {
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--port") && i + 1 < argc) g_port = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--fps") && i + 1 < argc) g_fps_target = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--quality") && i + 1 < argc) g_quality = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--subsamp") && i + 1 < argc) {
            g_subsamp = atoi(argv[++i]) == 422 ? TJSAMP_422 : TJSAMP_420;
        }
        else if (!strcmp(argv[i], "--device") && i + 1 < argc) g_device = argv[++i];
    }
    signal(SIGPIPE, SIG_IGN);
    if (drm_init() < 0) return 1;
    g_input_fd = input_connect();
    pthread_t cap;
    pthread_create(&cap, NULL, capture_loop, NULL);

    int srv = socket(AF_INET, SOCK_STREAM, 0);
    int one = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons(g_port);
    if (bind(srv, (struct sockaddr *)&addr, sizeof(addr)) < 0) { perror("bind"); return 1; }
    listen(srv, 16);
    printf("rkdesk-c listening on %d (%dx%d rot=%d, %d fps, q%d)\n",
           g_port, g_disp_w, g_disp_h, g_rotation, g_fps_target, g_quality);
    while (g_running) {
        int fd = accept(srv, NULL, NULL);
        if (fd < 0) continue;
        ConnArg *a = malloc(sizeof(ConnArg));
        a->fd = fd;
        pthread_t t;
        pthread_create(&t, NULL, conn_thread, a);
        pthread_detach(t);
    }
    close(srv);
    return 0;
}
