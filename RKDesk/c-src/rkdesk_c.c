/*
 * RKDesk C mirror - a performance-oriented screen mirror for RK3576.
 *
 * It captures the active DRM CRTC through a dma-buf, encodes the native
 * 800x1280 scanout with libjpeg-turbo and serves JPEG frames over a tiny
 * WebSocket server on port 8089.  The browser canvas rotates it to 1280x800.
 * Input events are forwarded to the existing rkdesk input daemon, so this
 * binary does not need uinput code of its own.
 *
 * It also provides the same PulseAudio audio path as the Python version:
 * /av carries 48 kHz PCM down and browser microphone PCM up, with a
 * C-specific virtual microphone (rkdesk_c_mic_source).  An optional HTTPS
 * listener (8444) provides the secure context required by getUserMedia.
 *
 * Build (Windows host, Zig):
 *   zig cc -target aarch64-linux-gnu -O3 -pthread \
 *       -L ../toolchains/board-libs \
 *       -l:libturbojpeg.so.0 -l:libssl.so.3 -l:libcrypto.so.3 \
 *       rkdesk_c.c -o rkdesk_c
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
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
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include <drm/drm.h>
#include <drm/drm_mode.h>

/* --- minimal OpenSSL ABI (libssl.so.3 / libcrypto.so.3 on the board) ----- */
typedef struct ssl_ctx_st SSL_CTX;
typedef struct ssl_st SSL;
typedef struct ssl_method_st SSL_METHOD;
#define SSL_FILETYPE_PEM 1
extern const SSL_METHOD *TLS_server_method(void);
extern SSL_CTX *SSL_CTX_new(const SSL_METHOD *);
extern void SSL_CTX_free(SSL_CTX *);
extern int SSL_CTX_use_certificate_file(SSL_CTX *, const char *, int);
extern int SSL_CTX_use_PrivateKey_file(SSL_CTX *, const char *, int);
extern int SSL_CTX_check_private_key(const SSL_CTX *);
extern SSL *SSL_new(SSL_CTX *);
extern int SSL_set_fd(SSL *, int);
extern int SSL_accept(SSL *);
extern int SSL_read(SSL *, void *, int);
extern int SSL_write(SSL *, const void *, int);
extern int SSL_shutdown(SSL *);
extern void SSL_free(SSL *);

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
static int g_https_port = 8444;
static const char *g_cert = "/opt/rkdesk/certs/rkdesk.crt";
static const char *g_key = "/opt/rkdesk/certs/rkdesk.key";
static SSL_CTX *g_ssl_ctx = NULL;
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
    SSL *ssl;
    int audio;
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
    if (c->ssl) {
        SSL_shutdown(c->ssl);
        SSL_free(c->ssl);
        c->ssl = NULL;
    }
    if (c->fd >= 0) close(c->fd);
    c->fd = -1;
    pthread_mutex_unlock(&c->lock);
    pthread_mutex_destroy(&c->lock);
    free(c);
}

static int conn_send_all(int fd, SSL *ssl, const void *data, size_t len) {
    const unsigned char *p = data;
    while (len) {
        int n = ssl ? SSL_write(ssl, p, (int)len)
                    : (int)send(fd, p, len, MSG_NOSIGNAL);
        if (n <= 0) return -1;
        p += n; len -= (size_t)n;
    }
    return 0;
}

static int conn_recv_exact(int fd, SSL *ssl, void *buf, size_t len) {
    unsigned char *p = buf;
    while (len) {
        int n = ssl ? SSL_read(ssl, p, (int)len)
                    : (int)recv(fd, p, len, 0);
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
    int rc = (c->fd >= 0) ? (conn_send_all(c->fd, c->ssl, head, hlen) ||
                             conn_send_all(c->fd, c->ssl, data, len)) : -1;
    pthread_mutex_unlock(&c->lock);
    return rc;
}

static int ws_send_text(Client *c, const char *text) {
    size_t len = strlen(text);
    unsigned char head[10];
    size_t hlen = 0;
    head[hlen++] = 0x81;
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
    int rc = (c->fd >= 0) ? (conn_send_all(c->fd, c->ssl, head, hlen) ||
                             conn_send_all(c->fd, c->ssl, text, len)) : -1;
    pthread_mutex_unlock(&c->lock);
    return rc;
}


/* --- audio (PulseAudio, PCM over /av) ------------------------------------ */
#define AUDIO_RATE 48000
#define AUDIO_FRAME 3840
#define MIC_FRAME 1920
#define C_MIC_SINK "rkdesk_c_mic"
#define C_MIC_SOURCE "rkdesk_c_mic_source"
#define C_MIC_PREV "/var/run/rkdesk-c-mic.previous"

static int g_pulse_ok = 0;
static char g_audio_source[256] = "";
static char g_audio_sources[8][256];
static char g_audio_labels[8][128];
static int g_audio_source_count = 0;
static pid_t g_audio_pid = -1;
static int g_audio_fd = -1;
static pthread_t g_audio_thread;
static volatile int g_audio_run = 0;
static int g_audio_clients = 0;
static pid_t g_mic_pid = -1;
static int g_mic_fd = -1;
static int g_mic_sink_module = -1;
static int g_mic_source_module = -1;
static char g_mic_previous[256] = "";
static Client *g_mic_owner = NULL;
static char g_audio_error[160] = "";

static int cmd_capture(const char *cmd, char *out, size_t size) {
    FILE *pipe = popen(cmd, "r");
    if (!pipe) return -1;
    size_t used = 0;
    while (used + 1 < size && fgets(out + used, (int)(size - used), pipe)) {
        used = strlen(out);
    }
    out[used] = 0;
    while (used && (out[used - 1] == '\n' || out[used - 1] == '\r')) out[--used] = 0;
    return pclose(pipe) == 0 ? 0 : -1;
}

static void audio_snapshot_sources(void) {
    g_audio_source_count = 0;
    g_audio_sources[0][0] = 0;
    FILE *pipe = popen("pactl list short sources 2>/dev/null", "r");
    if (!pipe) return;
    char line[512];
    while (fgets(line, sizeof(line), pipe) && g_audio_source_count < 8) {
        char *tab = strchr(line, '\t');
        if (!tab) continue;
        char *name = tab + 1;
        char *next = strchr(name, '\t');
        if (next) *next = 0;
        if (!name[0]) continue;
        snprintf(g_audio_sources[g_audio_source_count], 256, "%s", name);
        snprintf(g_audio_labels[g_audio_source_count], 128, "%s",
                 strstr(name, ".monitor") ? "系统声音" : "板端麦克风");
        g_audio_source_count++;
    }
    pclose(pipe);
}

static void audio_cleanup_modules(void) {
    FILE *pipe = popen("pactl list short modules 2>/dev/null", "r");
    if (!pipe) return;
    char line[512];
    int ids[16], count = 0;
    while (fgets(line, sizeof(line), pipe) && count < 16) {
        if (!strstr(line, "rkdesk_c_mic")) continue;
        int id = atoi(line);
        if (id > 0) ids[count++] = id;
    }
    pclose(pipe);
    for (int i = 0; i < count; i++) {
        char cmd[64];
        snprintf(cmd, sizeof(cmd), "pactl unload-module %d", ids[i]);
        if (system(cmd) != 0) { /* best effort */ }
    }
}

static void audio_init(void) {
    if (access("/tmp/pulse-socket", F_OK) == 0) {
        setenv("PULSE_SERVER", "unix:/tmp/pulse-socket", 1);
    }
    char info[256];
    if (cmd_capture("pactl info 2>/dev/null", info, sizeof(info)) != 0 || !info[0]) {
        g_pulse_ok = 0;
        return;
    }
    g_pulse_ok = 1;

    char previous[256] = "";
    FILE *handle = fopen(C_MIC_PREV, "r");
    if (handle) {
        if (fgets(previous, sizeof(previous), handle)) {
            char *nl = strpbrk(previous, "\r\n");
            if (nl) *nl = 0;
        }
        fclose(handle);
    }
    if (previous[0]) {
        char cmd[320];
        snprintf(cmd, sizeof(cmd), "pactl set-default-source '%s' 2>/dev/null", previous);
        if (system(cmd) != 0) { /* best effort */ }
        unlink(C_MIC_PREV);
    }
    audio_cleanup_modules();
    audio_snapshot_sources();

    char sink[256] = "", monitor[300] = "";
    if (cmd_capture("pactl get-default-sink 2>/dev/null", sink, sizeof(sink)) == 0 && sink[0]) {
        snprintf(monitor, sizeof(monitor), "%s.monitor", sink);
    }
    g_audio_source[0] = 0;
    for (int i = 0; i < g_audio_source_count; i++) {
        if (strcmp(g_audio_sources[i], monitor) == 0) {
            snprintf(g_audio_source, sizeof(g_audio_source), "%s", monitor);
            break;
        }
    }
    if (!g_audio_source[0]) {
        for (int i = 0; i < g_audio_source_count; i++) {
            if (strstr(g_audio_sources[i], ".monitor")) {
                snprintf(g_audio_source, sizeof(g_audio_source), "%s", g_audio_sources[i]);
                break;
            }
        }
    }
    if (!g_audio_source[0] && g_audio_source_count) {
        snprintf(g_audio_source, sizeof(g_audio_source), "%s", g_audio_sources[0]);
    }
}

static void audio_status_json(char *out, size_t size) {
    char sources[1024] = "";
    for (int i = 0; i < g_audio_source_count; i++) {
        if (i) strncat(sources, ",", sizeof(sources) - strlen(sources) - 1);
        strncat(sources, "{\"name\":\"", sizeof(sources) - strlen(sources) - 1);
        strncat(sources, g_audio_sources[i], sizeof(sources) - strlen(sources) - 1);
        strncat(sources, "\",\"label\":\"", sizeof(sources) - strlen(sources) - 1);
        strncat(sources, g_audio_labels[i], sizeof(sources) - strlen(sources) - 1);
        strncat(sources, "\"}", sizeof(sources) - strlen(sources) - 1);
    }
    snprintf(out, size,
             "{\"t\":\"av\",\"pulse\":%s,\"sources\":[%s],\"audio_source\":\"%s\","
             "\"virtual_mic\":%s,\"virtual_mic_supported\":%s,\"https_port\":%d,"
             "\"error\":\"%s\"}",
             g_pulse_ok ? "true" : "false", sources, g_audio_source,
             (g_mic_fd >= 0) ? "true" : "false", g_pulse_ok ? "true" : "false",
             g_https_port, g_audio_error);
}

static void audio_broadcast(const unsigned char *data, size_t len) {
    pthread_mutex_lock(&g_clients_lock);
    for (Client *c = g_clients; c; c = c->next) {
        if (c->audio) ws_send_binary(c, data, len);
    }
    pthread_mutex_unlock(&g_clients_lock);
}

static void *audio_capture_thread(void *arg) {
    (void)arg;
    unsigned char frame[AUDIO_FRAME];
    while (g_audio_run && g_audio_fd >= 0) {
        size_t got = 0;
        while (got < sizeof(frame) && g_audio_run) {
            ssize_t n = read(g_audio_fd, frame + got, sizeof(frame) - got);
            if (n <= 0) break;
            got += (size_t)n;
        }
        if (got < sizeof(frame)) break;
        audio_broadcast(frame, sizeof(frame));
    }
    g_audio_run = 0;
    return NULL;
}

static void audio_stop(void) {
    g_audio_run = 0;
    if (g_audio_pid > 0) {
        kill(g_audio_pid, SIGTERM);
        waitpid(g_audio_pid, NULL, 0);
        g_audio_pid = -1;
    }
    if (g_audio_fd >= 0) {
        close(g_audio_fd);
        g_audio_fd = -1;
    }
    if (g_audio_thread) {
        pthread_join(g_audio_thread, NULL);
        g_audio_thread = 0;
    }
}

static void audio_start(const char *source) {
    if (!g_pulse_ok) {
        snprintf(g_audio_error, sizeof(g_audio_error), "PulseAudio unavailable");
        return;
    }
    char selected[256];
    snprintf(selected, sizeof(selected), "%s", (source && source[0]) ? source : g_audio_source);
    int valid = 0;
    for (int i = 0; i < g_audio_source_count; i++) {
        if (strcmp(g_audio_sources[i], selected) == 0) { valid = 1; break; }
    }
    if (!valid) snprintf(selected, sizeof(selected), "%s", g_audio_source);
    if (!selected[0]) {
        snprintf(g_audio_error, sizeof(g_audio_error), "no audio source");
        return;
    }
    audio_stop();
    int pipefd[2];
    if (pipe(pipefd) != 0) return;
    pid_t pid = fork();
    if (pid == 0) {
        close(pipefd[0]);
        dup2(pipefd[1], STDOUT_FILENO);
        close(pipefd[1]);
        execlp("parec", "parec", "--device", selected, "--format=s16le",
               "--rate=48000", "--channels=2", "--raw", "--latency-msec=30",
               (char *)NULL);
        _exit(127);
    }
    if (pid < 0) {
        close(pipefd[0]); close(pipefd[1]);
        return;
    }
    close(pipefd[1]);
    g_audio_pid = pid;
    g_audio_fd = pipefd[0];
    g_audio_run = 1;
    snprintf(g_audio_source, sizeof(g_audio_source), "%s", selected);
    snprintf(g_audio_error, sizeof(g_audio_error), "");
    pthread_create(&g_audio_thread, NULL, audio_capture_thread, NULL);
}

static void audio_mic_stop(void) {
    if (g_mic_fd >= 0) {
        close(g_mic_fd);
        g_mic_fd = -1;
    }
    if (g_mic_pid > 0) {
        kill(g_mic_pid, SIGTERM);
        waitpid(g_mic_pid, NULL, 0);
        g_mic_pid = -1;
    }
    if (g_mic_source_module >= 0) {
        char cmd[64];
        snprintf(cmd, sizeof(cmd), "pactl unload-module %d", g_mic_source_module);
        if (system(cmd) != 0) { /* best effort */ }
        g_mic_source_module = -1;
    }
    if (g_mic_sink_module >= 0) {
        char cmd[64];
        snprintf(cmd, sizeof(cmd), "pactl unload-module %d", g_mic_sink_module);
        if (system(cmd) != 0) { /* best effort */ }
        g_mic_sink_module = -1;
    }
    if (g_mic_previous[0]) {
        char cmd[320];
        snprintf(cmd, sizeof(cmd), "pactl set-default-source '%s' 2>/dev/null", g_mic_previous);
        if (system(cmd) != 0) { /* best effort */ }
    }
    unlink(C_MIC_PREV);
    g_mic_owner = NULL;
    g_audio_error[0] = 0;
}

static int audio_mic_start(void) {
    if (!g_pulse_ok) {
        snprintf(g_audio_error, sizeof(g_audio_error), "PulseAudio unavailable");
        return 0;
    }
    if (g_mic_fd >= 0) return 1;

    char id[64];
    if (cmd_capture("pactl load-module module-null-sink sink_name=" C_MIC_SINK
                    " sink_properties=device.description=RKDesk_C_Microphone 2>/dev/null",
                    id, sizeof(id)) != 0 || !id[0]) {
        snprintf(g_audio_error, sizeof(g_audio_error), "load null sink failed");
        return 0;
    }
    g_mic_sink_module = atoi(id);
    if (cmd_capture("pactl load-module module-remap-source master=" C_MIC_SINK ".monitor"
                    " source_name=" C_MIC_SOURCE
                    " source_properties=device.description=RKDesk_C_Microphone 2>/dev/null",
                    id, sizeof(id)) != 0 || !id[0]) {
        snprintf(g_audio_error, sizeof(g_audio_error), "load remap source failed");
        audio_mic_stop();
        return 0;
    }
    g_mic_source_module = atoi(id);

    if (cmd_capture("pactl get-default-source 2>/dev/null", g_mic_previous,
                    sizeof(g_mic_previous)) != 0) {
        g_mic_previous[0] = 0;
    }
    FILE *prev = fopen(C_MIC_PREV, "w");
    if (prev) { fprintf(prev, "%s\n", g_mic_previous); fclose(prev); }
    if (system("pactl set-default-source " C_MIC_SOURCE " 2>/dev/null") != 0) {
        snprintf(g_audio_error, sizeof(g_audio_error), "set default source failed");
        audio_mic_stop();
        return 0;
    }

    int pipefd[2];
    if (pipe(pipefd) != 0) { audio_mic_stop(); return 0; }
    pid_t pid = fork();
    if (pid == 0) {
        close(pipefd[1]);
        dup2(pipefd[0], STDIN_FILENO);
        close(pipefd[0]);
        execlp("pacat", "pacat", "--device=" C_MIC_SINK, "--format=s16le",
               "--rate=48000", "--channels=1", "--raw", "--latency-msec=30",
               (char *)NULL);
        _exit(127);
    }
    if (pid < 0) {
        close(pipefd[0]); close(pipefd[1]);
        audio_mic_stop();
        return 0;
    }
    close(pipefd[0]);
    g_mic_pid = pid;
    g_mic_fd = pipefd[1];
    snprintf(g_audio_error, sizeof(g_audio_error), "");
    return 1;
}

static void audio_mic_write(const unsigned char *data, size_t len) {
    if (g_mic_fd < 0) return;
    size_t sent = 0;
    while (sent < len) {
        ssize_t n = write(g_mic_fd, data + sent, len - sent);
        if (n <= 0) { audio_mic_stop(); return; }
        sent += (size_t)n;
    }
}

static void audio_test_tone(void) {
    if (!g_pulse_ok || !g_audio_source[0]) return;
    char sink[256];
    if (cmd_capture("pactl get-default-sink 2>/dev/null", sink, sizeof(sink)) != 0 ||
        !sink[0]) return;
    int pipefd[2];
    if (pipe(pipefd) != 0) return;
    pid_t pid = fork();
    if (pid == 0) {
        close(pipefd[1]);
        dup2(pipefd[0], STDIN_FILENO);
        close(pipefd[0]);
        execlp("pacat", "pacat", "--device", sink, "--format=s16le",
               "--rate=48000", "--channels=2", "--raw", (char *)NULL);
        _exit(127);
    }
    if (pid < 0) { close(pipefd[0]); close(pipefd[1]); return; }
    close(pipefd[0]);
    int frames = 48000 * 6 / 5;
    for (int i = 0; i < frames; i++) {
        short value = (short)(0.25 * 32767.0 * sin(2.0 * 3.141592653589793 * 440.0 * i / 48000.0));
        short pair[2] = {value, value};
        if (write(pipefd[1], pair, sizeof(pair)) != (ssize_t)sizeof(pair)) break;
    }
    close(pipefd[1]);
    waitpid(pid, NULL, 0);
}

static void audio_handle(Client *client, const char *json) {
    char source[256] = "";
    char *p = strstr(json, "\"source\":\"");
    if (p) {
        p += 10;
        char *end = strchr(p, '"');
        if (end && (size_t)(end - p) < sizeof(source)) {
            memcpy(source, p, (size_t)(end - p));
            source[end - p] = 0;
        }
    }
    if (strstr(json, "\"audio_start\"")) {
        audio_start(source);
        char status[2048];
        audio_status_json(status, sizeof(status));
        ws_send_text(client, status);
    } else if (strstr(json, "\"audio_stop\"")) {
        audio_stop();
        char status[2048];
        audio_status_json(status, sizeof(status));
        ws_send_text(client, status);
    } else if (strstr(json, "\"mic_start\"")) {
        if (audio_mic_start()) {
            g_mic_owner = client;
        }
        char status[2048];
        audio_status_json(status, sizeof(status));
        ws_send_text(client, status);
    } else if (strstr(json, "\"mic_stop\"")) {
        audio_mic_stop();
        char status[2048];
        audio_status_json(status, sizeof(status));
        ws_send_text(client, status);
    } else if (strstr(json, "\"test_tone\"")) {
        audio_test_tone();
    } else if (strstr(json, "\"status\"")) {
        char status[2048];
        audio_status_json(status, sizeof(status));
        ws_send_text(client, status);
    }
}

static void audio_client_add(Client *client) {
    __sync_fetch_and_add(&g_audio_clients, 1);
    char status[2048];
    audio_status_json(status, sizeof(status));
    ws_send_text(client, status);
}

static void audio_client_remove(Client *client) {
    int owner = (client == g_mic_owner);
    if (owner) audio_mic_stop();
    int left = __sync_sub_and_fetch(&g_audio_clients, 1);
    if (left <= 0) {
        audio_stop();
        audio_mic_stop();
    }
}

static void audio_shutdown(void) {
    audio_stop();
    audio_mic_stop();
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
                if (!c->audio) ws_send_binary(c, jpeg, size);
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
"header{height:48px;display:flex;align-items:center;gap:12px;padding:0 18px;background:#0b141b;border-bottom:1px solid #1d2b37}"
"b{color:#37e0b0}.sub{color:#8397a8;font-size:12px}#hud{margin-left:10px;font:11px monospace;color:#8397a8}#hud span{margin-left:14px}"
"#av{margin-left:auto;display:flex;align-items:center;gap:5px}"
"#av .ab{height:26px;padding:0 9px;border-radius:6px;border:1px solid #243746;background:#111d26;color:#c9d7e2;font-size:11px;cursor:pointer}"
"#av .ab.on{color:#37e0b0;border-color:#2b8f78;background:#0e2a25}"
"#av .ab:disabled{opacity:.4;cursor:not-allowed}"
"#av select{height:26px;max-width:190px;background:#111d26;color:#c9d7e2;border:1px solid #243746;border-radius:6px;font-size:11px}"
"#astat{font:10px monospace;color:#8397a8;white-space:nowrap}"
"main{height:calc(100% - 48px);display:grid;place-items:center;padding:14px}"
"canvas{max-width:100%;max-height:100%;border-radius:10px;box-shadow:0 20px 60px #000a;cursor:none;touch-action:none;background:#000}"
"#k{position:fixed;left:-1000px;top:0;opacity:0}"
"</style></head><body><header><b>RK3576 C mirror</b><span>DRM + TurboJPEG · port 8089</span>"
"<div id=av><button id=spk class=ab>声音</button><select id=asrc></select><button id=mic class=ab>麦克风</button><button id=tone class=ab>测试音</button><span id=astat>音频检测中</span></div>"
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
"const spk=document.getElementById('spk'),micb=document.getElementById('mic'),toneb=document.getElementById('tone'),asrc=document.getElementById('asrc'),astat=document.getElementById('astat');"
"let avws,actx,again,anext=0,aplaying=false,amic=false,astream=null,anode=null,ag=1;"
"function avSend(o){if(avws&&avws.readyState===1)avws.send(JSON.stringify(o));}"
"function avConnect(){avws=new WebSocket((location.protocol==='https:'?'wss':'ws')+'://'+location.host+'/av');avws.binaryType='arraybuffer';"
"avws.onopen=()=>{astat.textContent='音频已连接';if(aplaying)avSend({t:'audio_start',source:asrc.value});if(amic)avSend({t:'mic_start'});};avws.onclose=()=>{astat.textContent='音频断开';setTimeout(avConnect,1500);};"
"avws.onmessage=e=>{if(typeof e.data==='string'){try{let s=JSON.parse(e.data);if(s.t==='av')avStatus(s);}catch(err){}return;}if(aplaying)playPcm(e.data);};}"
"function avStatus(s){let names=(s.sources||[]).map(x=>x.name).join('|');if(asrc.dataset.sig!==names){asrc.innerHTML='';(s.sources||[]).forEach(x=>{let o=document.createElement('option');o.value=x.name;o.textContent=x.label;asrc.appendChild(o);});asrc.dataset.sig=names;}"
"if(s.audio_source&&[...asrc.options].some(o=>o.value===s.audio_source))asrc.value=s.audio_source;"
"if(s.error){if(amic)micStop(false);astat.textContent=s.error;}"
"micb.disabled=!s.virtual_mic_supported;"
"astat.textContent=s.pulse?(s.virtual_mic?'麦克风已启用':'PulseAudio'):'音频不可用';"
"if(!window.isSecureContext){astat.textContent='麦克风需 HTTPS:8444';}}"
"async function actxInit(){if(!actx){let C=window.AudioContext||window.webkitAudioContext;actx=new C({sampleRate:48000});again=actx.createGain();again.gain.value=0.8;again.connect(actx.destination);}if(actx.state==='suspended')await actx.resume();}"
"async function audioStart(){try{await actxInit();}catch(e){astat.textContent='AudioContext 失败';return;}aplaying=true;anext=0;spk.classList.add('on');spk.textContent='停止';avSend({t:'audio_start',source:asrc.value});}"
"function audioStop(){aplaying=false;spk.classList.remove('on');spk.textContent='声音';avSend({t:'audio_stop'});}"
"function playPcm(buf){if(!actx||!aplaying)return;let s=new Int16Array(buf),n=Math.floor(s.length/2);if(!n)return;let b=actx.createBuffer(2,n,48000),l=b.getChannelData(0),r=b.getChannelData(1);for(let i=0;i<n;i++){l[i]=s[i*2]/32768;r[i]=s[i*2+1]/32768;}let src=actx.createBufferSource();src.buffer=b;src.connect(again);let now=actx.currentTime;if(anext<now+0.03||anext>now+0.8)anext=now+0.08;src.start(anext);anext+=b.duration;}"
"async function micStart(){if(!window.isSecureContext){location.href='https://'+location.hostname+':8444/';return;}try{astream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true}});await actxInit();"
"let code=`class R extends AudioWorkletProcessor{constructor(){super();this.b=[]}process(i){let c=i[0][0];if(!c)return true;for(let k=0;k<c.length;k++){this.b.push(c[k]);if(this.b.length>=960){this.port.postMessage(new Float32Array(this.b));this.b=[]}}return true}}registerProcessor('rkdesk-mic',R)`;"
"let u=URL.createObjectURL(new Blob([code],{type:'application/javascript'}));await actx.audioWorklet.addModule(u);URL.revokeObjectURL(u);"
"let src=actx.createMediaStreamSource(astream);anode=new AudioWorkletNode(actx,'rkdesk-mic');src.connect(anode);let z=actx.createGain();z.gain.value=0;anode.connect(z);z.connect(actx.destination);"
"anode.port.onmessage=e=>{if(!amic)return;let f=e.data,p=new Int16Array(f.length);for(let i=0;i<f.length;i++){let v=Math.max(-1,Math.min(1,f[i]*ag));p[i]=v*32767;}if(avws&&avws.readyState===1)avws.send(p.buffer);};"
"amic=true;micb.classList.add('on');micb.textContent='停止麦克风';avSend({t:'mic_start'});astat.textContent='麦克风发送中';}catch(e){astat.textContent='麦克风失败';micStop(false);}}"
"function micStop(send){amic=false;micb.classList.remove('on');micb.textContent='麦克风';if(anode){try{anode.disconnect();}catch(e){}anode=null;}if(astream){astream.getTracks().forEach(t=>t.stop());astream=null;}if(send!==false)avSend({t:'mic_stop'});astat.textContent='麦克风未启用';}"
"spk.onclick=()=>{if(aplaying)audioStop();else audioStart();};"
"micb.onclick=()=>{if(amic)micStop();else micStart();};"
"toneb.onclick=()=>avSend({t:'test_tone'});"
"asrc.onchange=()=>{if(aplaying)avSend({t:'audio_start',source:asrc.value});};"
"fit();connect();avConnect();</script></body></html>";

static void http_response(int fd, SSL *ssl, int code, const char *type,
                          const void *body, size_t len) {
    char head[256];
    const char *reason = code == 200 ? "OK" : code == 404 ? "Not Found" : "Error";
    int n = snprintf(head, sizeof(head),
                     "HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %zu\r\n"
                     "Cache-Control: no-store\r\nConnection: close\r\n\r\n",
                     code, reason, type, len);
    conn_send_all(fd, ssl, head, n);
    if (len) conn_send_all(fd, ssl, body, len);
}

static int conn_read(int fd, SSL *ssl, void *buf, size_t len) {
    return ssl ? SSL_read(ssl, buf, (int)len) : (int)recv(fd, buf, len, 0);
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

static void ws_handshake(int fd, SSL *ssl, char *key) {
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
    conn_send_all(fd, ssl, head, n);
}

static int ws_read_frame(int fd, SSL *ssl, unsigned char *opcode,
                         unsigned char **payload, uint64_t *plen) {
    unsigned char h[2];
    if (conn_recv_exact(fd, ssl, h, 2) != 0) return -1;
    *opcode = h[0] & 0x0F;
    int masked = h[1] & 0x80;
    uint64_t len = h[1] & 0x7F;
    if (len == 126) {
        unsigned char b[2];
        if (conn_recv_exact(fd, ssl, b, 2) != 0) return -1;
        len = ((uint64_t)b[0] << 8) | b[1];
    } else if (len == 127) {
        unsigned char b[8];
        if (conn_recv_exact(fd, ssl, b, 8) != 0) return -1;
        len = 0;
        for (int i = 0; i < 8; i++) len = (len << 8) | b[i];
    }
    if (len > 65536) return -1;
    unsigned char mask[4] = {0, 0, 0, 0};
    if (masked && conn_recv_exact(fd, ssl, mask, 4) != 0) return -1;
    unsigned char *buf = malloc(len + 1);
    if (!buf) return -1;
    if (len && conn_recv_exact(fd, ssl, buf, len) != 0) { free(buf); return -1; }
    if (masked) for (uint64_t i = 0; i < len; i++) buf[i] ^= mask[i % 4];
    buf[len] = 0;
    *payload = buf;
    *plen = len;
    return 0;
}

typedef struct { int fd; int tls; } ConnArg;

static void *conn_thread(void *arg) {
    ConnArg *ca = (ConnArg *)arg;
    int fd = ca->fd;
    int tls = ca->tls;
    free(ca);
    SSL *ssl = NULL;
    Client *client = NULL;
    struct timeval tv = {1, 0};
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

    if (tls) {
        if (!g_ssl_ctx) { close(fd); return NULL; }
        ssl = SSL_new(g_ssl_ctx);
        if (!ssl || SSL_set_fd(ssl, fd) != 1 || SSL_accept(ssl) != 1) {
            if (ssl) SSL_free(ssl);
            close(fd);
            return NULL;
        }
    }

    char req[8192];
    size_t used = 0;
    while (used < sizeof(req) - 1) {
        int n = conn_read(fd, ssl, req + used, sizeof(req) - 1 - used);
        if (n <= 0) goto cleanup;
        used += (size_t)n;
        if (strstr(req, "\r\n\r\n")) break;
    }
    req[used] = 0;
    char method[8], path[256];
    if (sscanf(req, "%7s %255s", method, path) != 2) goto cleanup;

    if (strcmp(path, "/healthz") == 0) {
        char body[320];
        int n = snprintf(body, sizeof(body),
                         "{\"ok\":true,\"c\":true,\"width\":%d,\"height\":%d,\"rotation\":%d,"
                         "\"fps\":%.1f,\"encode_ms\":%.1f,\"audio\":%s,"
                         "\"https_port\":%d}\n",
                         g_disp_w, g_disp_h, g_rotation, g_fps, g_encode_ms,
                         g_pulse_ok ? "true" : "false", g_https_port);
        http_response(fd, ssl, 200, "application/json", body, n);
        goto cleanup;
    }
    if (strcmp(path, "/api/av") == 0) {
        char body[2048];
        audio_status_json(body, sizeof(body));
        http_response(fd, ssl, 200, "application/json", body, strlen(body));
        goto cleanup;
    }
    if (strcmp(path, "/api/screenshot.jpg") == 0) {
        pthread_mutex_lock(&g_frame_lock);
        size_t len = g_latest_size;
        unsigned char *copy = len ? malloc(len) : NULL;
        if (copy) memcpy(copy, g_latest, len);
        pthread_mutex_unlock(&g_frame_lock);
        if (!copy) {
            http_response(fd, ssl, 503, "text/plain", "no frame", 8);
            goto cleanup;
        }
        http_response(fd, ssl, 200, "image/jpeg", copy, len);
        free(copy);
        goto cleanup;
    }
    if (strcmp(path, "/ws") == 0 || strcmp(path, "/av") == 0) {
        int audio = strcmp(path, "/av") == 0;
        char *key = find_header(req, "Sec-WebSocket-Key:");
        char *up = find_header(req, "Upgrade:");
        if (!key || !up || !strcasestr(up, "websocket")) {
            http_response(fd, ssl, 400, "text/plain", "websocket required", 18);
            goto cleanup;
        }
        char *end = key;
        while (*end && *end != '\r' && *end != '\n') end++;
        *end = 0;
        ws_handshake(fd, ssl, key);
        client = calloc(1, sizeof(Client));
        client->fd = fd;
        client->ssl = ssl;
        client->audio = audio;
        pthread_mutex_init(&client->lock, NULL);
        client_add(client);
        if (audio) audio_client_add(client);
        ssl = NULL; /* owned by client */

        while (g_running) {
            unsigned char opcode;
            unsigned char *payload = NULL;
            uint64_t len = 0;
            if (ws_read_frame(fd, client->ssl, &opcode, &payload, &len) < 0) break;
            if (opcode == 0x8) { free(payload); break; }
            if (opcode == 0x9) {
                unsigned char pong[2] = {0x8A, 0};
                conn_send_all(fd, client->ssl, pong, 2);
            } else if (opcode == 0x1) {
                if (audio) audio_handle(client, (const char *)payload);
                else input_forward((const char *)payload, len);
            } else if (opcode == 0x2 && audio) {
                if (len >= 2 && len <= MIC_FRAME * 8) audio_mic_write(payload, len);
            }
            free(payload);
        }
        if (audio) audio_client_remove(client);
        client_remove(client);
        client = NULL;
        return NULL;
    }

    http_response(fd, ssl, 200, "text/html; charset=utf-8", PAGE, strlen(PAGE));

cleanup:
    if (client) {
        if (client->audio) audio_client_remove(client);
        client_remove(client);
        return NULL;
    }
    if (ssl) {
        SSL_shutdown(ssl);
        SSL_free(ssl);
    }
    close(fd);
    return NULL;
}

typedef struct { int port; int tls; } ListenArg;

static void *listener_thread(void *arg) {
    ListenArg *la = (ListenArg *)arg;
    int port = la->port;
    int tls = la->tls;
    free(la);
    int srv = socket(AF_INET, SOCK_STREAM, 0);
    if (srv < 0) return NULL;
    int one = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons(port);
    if (bind(srv, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("bind");
        close(srv);
        return NULL;
    }
    listen(srv, 16);
    while (g_running) {
        int fd = accept(srv, NULL, NULL);
        if (fd < 0) continue;
        ConnArg *ca = malloc(sizeof(ConnArg));
        ca->fd = fd;
        ca->tls = tls;
        pthread_t t;
        if (pthread_create(&t, NULL, conn_thread, ca) == 0) pthread_detach(t);
        else { close(fd); free(ca); }
    }
    close(srv);
    return NULL;
}

int main(int argc, char **argv) {
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--port") && i + 1 < argc) g_port = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--https-port") && i + 1 < argc) g_https_port = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--fps") && i + 1 < argc) g_fps_target = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--quality") && i + 1 < argc) g_quality = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--subsamp") && i + 1 < argc) {
            g_subsamp = atoi(argv[++i]) == 422 ? TJSAMP_422 : TJSAMP_420;
        }
        else if (!strcmp(argv[i], "--device") && i + 1 < argc) g_device = argv[++i];
        else if (!strcmp(argv[i], "--cert") && i + 1 < argc) g_cert = argv[++i];
        else if (!strcmp(argv[i], "--key") && i + 1 < argc) g_key = argv[++i];
    }
    signal(SIGPIPE, SIG_IGN);
    if (drm_init() < 0) return 1;
    audio_init();
    atexit(audio_shutdown);
    g_input_fd = input_connect();
    pthread_t cap;
    pthread_create(&cap, NULL, capture_loop, NULL);

    if (g_https_port && access(g_cert, F_OK) == 0 && access(g_key, F_OK) == 0) {
        g_ssl_ctx = SSL_CTX_new(TLS_server_method());
        if (!g_ssl_ctx ||
            SSL_CTX_use_certificate_file(g_ssl_ctx, g_cert, SSL_FILETYPE_PEM) != 1 ||
            SSL_CTX_use_PrivateKey_file(g_ssl_ctx, g_key, SSL_FILETYPE_PEM) != 1 ||
            SSL_CTX_check_private_key(g_ssl_ctx) != 1) {
            fprintf(stderr, "TLS setup failed; HTTPS disabled\n");
            if (g_ssl_ctx) { SSL_CTX_free(g_ssl_ctx); g_ssl_ctx = NULL; }
        }
    }

    ListenArg *http_arg = malloc(sizeof(ListenArg));
    http_arg->port = g_port;
    http_arg->tls = 0;
    pthread_t http_thread;
    pthread_create(&http_thread, NULL, listener_thread, http_arg);

    pthread_t https_thread;
    if (g_ssl_ctx) {
        ListenArg *https_arg = malloc(sizeof(ListenArg));
        https_arg->port = g_https_port;
        https_arg->tls = 1;
        pthread_create(&https_thread, NULL, listener_thread, https_arg);
        printf("rkdesk-c http %d, https %d (%dx%d rot=%d, %d fps, q%d)\n",
               g_port, g_https_port, g_disp_w, g_disp_h, g_rotation,
               g_fps_target, g_quality);
    } else {
        printf("rkdesk-c http %d (TLS disabled) (%dx%d rot=%d, %d fps, q%d)\n",
               g_port, g_disp_w, g_disp_h, g_rotation, g_fps_target, g_quality);
    }
    printf("audio: %s, source=%s\n", g_pulse_ok ? "pulse" : "unavailable", g_audio_source);

    while (g_running) sleep(1);
    if (g_ssl_ctx) SSL_CTX_free(g_ssl_ctx);
    return 0;
}
