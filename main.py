# Just pip install the requirements and pray the PyAV binaries don't segfault.
import glfw
import OpenGL.GL as gl
import imgui
from imgui.integrations.glfw import GlfwRenderer

import sys
import os
import subprocess
import time
import threading
import socket
import random
import struct
import io
import urllib.request
import zipfile
import shutil

import numpy as np
import av
import pyaudio

base_dir = os.path.dirname(os.path.abspath(__file__))

adb_path    = os.path.join(base_dir, "scrcpy", "adb.exe")
server_path = os.path.join(base_dir, "scrcpy", "scrcpy-server")

# Do NOT touch this version string unless you are updating the .jar file alongside it. 
# It will silently fail and you will waste 3 hours debugging.
SERVER_VERSION = "3.3.4"
DEVICE_SERVER_PATH = "/data/local/tmp/scrcpy-server.jar"

# Just a random port. Hopefully no other app is squatting on this.
TUNNEL_PORT = 27183

# Global state dictionary. Because passing state down 15 levels of UI code is for masochists.
state = {
    "show_touch":         False,
    "ai_chart":           False,
    "delay":              0.0,
    "current_file_idx":   0,
    "files":              ["None"],
    "scrcpy_status":      "INIT",
    "devices":            ["None"],
    "current_device_idx": 0,
    "resolutions": ["Max", "1920", "1440", "1080", "720", "480"],
    "current_res_idx":    0,
    "max_fps":            60,
}

_device_serial = None
_server_proc = None

# Thread safety?
_frame_lock   = threading.Lock()
_latest_frame = None
_video_thread = None
_stop_video   = threading.Event()

_audio_thread = None
_stop_audio   = threading.Event()
_pa           = None
_pa_stream    = None

AUDIO_RATE     = 48000
AUDIO_CHANNELS = 2
AUDIO_CHUNK    = 1024

_tex_id = None
_tex_w  = 0
_tex_h  = 0

def _fetch_scrcpy_dependencies():
    if os.path.exists(adb_path) and os.path.exists(server_path):
        return

    print("[BOOT] Missing scrcpy binaries. Initiating payload delivery...")
    os.makedirs(os.path.dirname(adb_path), exist_ok=True)
    
    zip_url = f"https://github.com/Genymobile/scrcpy/releases/download/v{SERVER_VERSION}/scrcpy-win64-v{SERVER_VERSION}.zip"
    zip_path = os.path.join(base_dir, "scrcpy_temp.zip")

    try:
        print(f"[BOOT] Downloading scrcpy v{SERVER_VERSION} (this might take a sec)...")
        # Masquerade as a normal browser because sometimes GitHub blocks bare python-urllib
        req = urllib.request.Request(zip_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response, open(zip_path, 'wb') as out_file:
            shutil.copyfileobj(response, out_file)

        print("[BOOT] Unzipping the goods...")
        with zipfile.ZipFile(zip_path, 'r') as z:
            # only care about the server, ADB, and its life-support DLLs. Everything else is bloat.
            target_files = ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll", "scrcpy-server")
            for file_info in z.infolist():
                filename = os.path.basename(file_info.filename)
                
                if filename in target_files:
                    source = z.open(file_info)
                    target_path = os.path.join(base_dir, "scrcpy", filename)
                    with source, open(target_path, "wb") as target:
                        shutil.copyfileobj(source, target)
        
        os.remove(zip_path) # Clean up the evidence
        print("[BOOT] Dependencies acquired.")
        
    except Exception as e:
        print(f"[BOOT] Downloader completely shit the bed: {e}")
        print(f"[BOOT] You'll have to download it manually from {zip_url} and extract it to the 'scrcpy' folder.")
        sys.exit(1)


def _si():
    # theres a meme where if cmd popup and hide, you're hacked. you're gay if you think that.
    # hide cmd
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return si

def _adb(*args, capture=True) -> str:
    # fallback to system if some maniac deleted it
    cmd =[adb_path if os.path.exists(adb_path) else "adb"]
    if _device_serial:
        cmd +=["-s", _device_serial]
    cmd += list(args)
    if capture:
        return subprocess.check_output(cmd, text=True,
                                       startupinfo=_si(),
                                       stderr=subprocess.DEVNULL)
    else:
        return subprocess.call(cmd, startupinfo=_si(),
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)

def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    # If the socket dies halfway through a payload, Its fucked anyway.
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Socket gave up on life")
        buf += chunk
    return buf

# Shaders. Standard BT.601 conversion. Don't touch the floats unless you want green smurfs.
_VERT_SRC = """
#version 330 core
in  vec2 position;
in  vec2 texcoord;
out vec2 v_texcoord;
void main() {
    gl_Position = vec4(position, 0.0, 1.0);
    v_texcoord  = texcoord;
}
"""

_FRAG_SRC = """
#version 330 core
uniform sampler2D tex_y;
uniform sampler2D tex_u;
uniform sampler2D tex_v;
in  vec2 v_texcoord;
out vec4 out_color;
void main() {
    float y = texture(tex_y, v_texcoord).r;
    float u = texture(tex_u, v_texcoord).r - 0.5;
    float v = texture(tex_v, v_texcoord).r - 0.5;
    float r = clamp(y + 1.402  * v,           0.0, 1.0);
    float g = clamp(y - 0.3441 * u - 0.7141 * v, 0.0, 1.0);
    float b = clamp(y + 1.772  * u,           0.0, 1.0);
    out_color = vec4(r, g, b, 1.0);
}
"""

_yuv_prog    = None
_yuv_vao     = None
_tex_y       = None
_tex_u       = None
_tex_v       = None
_yuv_w       = 0
_yuv_h       = 0

_tex_id      = None
_tex_w       = 0
_tex_h       = 0

_fbo         = None
_latest_frame = None

def _compile_shader(src: str, kind: int) -> int:
    shader = gl.glCreateShader(kind)
    gl.glShaderSource(shader, src)
    gl.glCompileShader(shader)
    if not gl.glGetShaderiv(shader, gl.GL_COMPILE_STATUS):
        raise RuntimeError(f"Shader refused to compile: {gl.glGetShaderInfoLog(shader)}")
    return shader

def _init_yuv_pipeline():
    # I love writing 60 lines of OpenGL boilerplate just to draw a fucking square
    global _yuv_prog, _yuv_vao, _tex_y, _tex_u, _tex_v
    global _tex_id, _fbo

    vert = _compile_shader(_VERT_SRC, gl.GL_VERTEX_SHADER)
    frag = _compile_shader(_FRAG_SRC, gl.GL_FRAGMENT_SHADER)
    prog = gl.glCreateProgram()
    gl.glAttachShader(prog, vert)
    gl.glAttachShader(prog, frag)
    gl.glLinkProgram(prog)
    if not gl.glGetProgramiv(prog, gl.GL_LINK_STATUS):
        raise RuntimeError(f"Shader link failed. Why: {gl.glGetProgramInfoLog(prog)}")
    gl.glDeleteShader(vert)
    gl.glDeleteShader(frag)
    _yuv_prog = prog

    verts = np.array([
        -1, -1,  0, 0,
         1, -1,  1, 0,
         1,  1,  1, 1,
        -1,  1,  0, 1,
    ], dtype=np.float32)
    idx = np.array([0, 1, 2, 0, 2, 3], dtype=np.uint32)

    vao = gl.glGenVertexArrays(1)
    vbo = gl.glGenBuffers(1)
    ebo = gl.glGenBuffers(1)

    gl.glBindVertexArray(vao)
    gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
    gl.glBufferData(gl.GL_ARRAY_BUFFER, verts.nbytes, verts, gl.GL_STATIC_DRAW)
    gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, ebo)
    gl.glBufferData(gl.GL_ELEMENT_ARRAY_BUFFER, idx.nbytes, idx, gl.GL_STATIC_DRAW)

    pos_loc = gl.glGetAttribLocation(prog, "position")
    tex_loc = gl.glGetAttribLocation(prog, "texcoord")
    gl.glEnableVertexAttribArray(pos_loc)
    gl.glVertexAttribPointer(pos_loc, 2, gl.GL_FLOAT, False, 16, gl.ctypes.c_void_p(0))
    gl.glEnableVertexAttribArray(tex_loc)
    gl.glVertexAttribPointer(tex_loc, 2, gl.GL_FLOAT, False, 16, gl.ctypes.c_void_p(8))
    gl.glBindVertexArray(0)
    _yuv_vao = vao

    def make_tex():
        t = gl.glGenTextures(1)
        gl.glBindTexture(gl.GL_TEXTURE_2D, t)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        return t

    _tex_y = make_tex()
    _tex_u = make_tex()
    _tex_v = make_tex()

    _tex_id = make_tex()
    _fbo    = gl.glGenFramebuffers(1)
    print("[GL] Shader pipeline actually survived initialization.")

def _upload_yuv_and_render(yuv: dict):
    # Ramming raw memory buffers straight into the GPU. 
    global _yuv_w, _yuv_h, _tex_w, _tex_h

    y_plane = yuv["y"]
    u_plane = yuv["u"]
    v_plane = yuv["v"]
    w, h    = yuv["w"], yuv["h"]
    hw, hh  = w // 2, h // 2

    gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)

    def upload(tex, data, tw, th, alloc):
        gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
        if alloc:
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RED, tw, th, 0,
                            gl.GL_RED, gl.GL_UNSIGNED_BYTE, data.tobytes())
        else:
            gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, tw, th,
                               gl.GL_RED, gl.GL_UNSIGNED_BYTE, data.tobytes())

    alloc = (w != _yuv_w or h != _yuv_h)

    upload(_tex_y, y_plane, w,  h,  alloc)
    upload(_tex_u, u_plane, hw, hh, alloc)
    upload(_tex_v, v_plane, hw, hh, alloc)

    # Re-allocating FBO if the device rotates or resolution changes mid-stream
    if alloc:
        gl.glBindTexture(gl.GL_TEXTURE_2D, _tex_id)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGB, w, h, 0,
                        gl.GL_RGB, gl.GL_UNSIGNED_BYTE, None)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, _fbo)
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                  gl.GL_TEXTURE_2D, _tex_id, 0)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        _yuv_w, _yuv_h = w, h
        _tex_w, _tex_h = w, h

    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, _fbo)
    gl.glViewport(0, 0, w, h)
    gl.glUseProgram(_yuv_prog)

    for unit, tex, name in[(0, _tex_y, "tex_y"),
                             (1, _tex_u, "tex_u"),
                             (2, _tex_v, "tex_v")]:
        gl.glActiveTexture(gl.GL_TEXTURE0 + unit)
        gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
        gl.glUniform1i(gl.glGetUniformLocation(_yuv_prog, name), unit)

    gl.glBindVertexArray(_yuv_vao)
    gl.glDrawElements(gl.GL_TRIANGLES, 6, gl.GL_UNSIGNED_INT, None)
    gl.glBindVertexArray(0)
    gl.glUseProgram(0)
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)

    io = imgui.get_io()
    gl.glViewport(0, 0, int(io.display_size.x), int(io.display_size.y))

def _adb_worker():
    # Scraping adb stdout because Google refuses to provide a decent machine-readable format
    global _device_serial
    adb_bin = adb_path if os.path.exists(adb_path) else "adb"
    try:
        out   = subprocess.check_output([adb_bin, "devices"],
                                        text=True, startupinfo=_si())
        lines = out.strip().split("\n")[1:]
        devices = [ln.split()[0] for ln in lines
                   if "device" in ln and "offline" not in ln]
        if devices:
            state["devices"]            = devices
            state["current_device_idx"] = 0
            _device_serial              = devices[0]
            state["scrcpy_status"]      = "READY"
        else:
            state["devices"]            = ["None"]
            state["current_device_idx"] = 0
            state["scrcpy_status"]      = "NO_DEVICE"
    except Exception as e:
        print(f"[ADB] Dumpstered: {e}")
        state["devices"]       = ["None"]
        state["scrcpy_status"] = "NO_DEVICE"

def check_adb_devices():
    state["scrcpy_status"] = "STARTING_ADB"
    threading.Thread(target=_adb_worker, daemon=True).start()

def _push_server():
    if not os.path.exists(server_path):
        print(f"[SERVER] Bruh, where is the jar? Missing at {server_path}")
        return False
    try:
        _adb("push", server_path, DEVICE_SERVER_PATH, capture=False)
        print(f"[SERVER] Yeeted {server_path} -> {DEVICE_SERVER_PATH}")
        return True
    except Exception as e:
        print(f"[SERVER] ADB push failed: {e}")
        return False

def _start_server(scid: int, res: str, max_fps: int = 60) -> subprocess.Popen | None:
    # The arcane incantation to make app_process execute a java class directly on the phone.
    # turn OFF control and ON raw_stream=false so it actually get packet headers.
    adb_bin = adb_path if os.path.exists(adb_path) else "adb"

    max_size = res if res != "Max" else "0"
    scid_hex = format(scid, "08x")

    server_args = " ".join([
        f"CLASSPATH={DEVICE_SERVER_PATH}",
        "app_process",
        "/",
        "com.genymobile.scrcpy.Server",
        SERVER_VERSION,
        f"scid={scid_hex}",
        "tunnel_forward=true",
        f"max_size={max_size}",
        f"max_fps={max_fps}",
        "video=true",
        "audio=true",
        "control=false",
        "raw_stream=false", 
        "send_dummy_byte=true",
        "send_device_meta=true",
        "send_frame_meta=true",
        "audio_codec=aac",
        "video_codec=h264",
        "video_encoder=c2.exynos.h264.encoder",
        "cleanup=true",
    ])

    cmd =[adb_bin]
    if _device_serial:
        cmd += ["-s", _device_serial]
    cmd += ["shell", server_args]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            startupinfo=_si(),
        )
        print(f"[SERVER] Sent the jar to the shadow realm. PID={proc.pid}")
        return proc
    except Exception as e:
        print(f"[SERVER] App_process completely shit the bed: {e}")
        return None

def _setup_tunnel(scid: int) -> bool:
    adb_bin = adb_path if os.path.exists(adb_path) else "adb"
    abstract = f"localabstract:scrcpy_{format(scid, '08x')}"
    cmd = [adb_bin]
    if _device_serial:
        cmd += ["-s", _device_serial]
    cmd +=["forward", f"tcp:{TUNNEL_PORT}", abstract]
    try:
        subprocess.check_call(cmd, startupinfo=_si(),
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        print(f"[TUNNEL] Port forward failed. Are we already bound?: {e}")
        return False

def _remove_tunnel():
    adb_bin = adb_path if os.path.exists(adb_path) else "adb"
    cmd = [adb_bin]
    if _device_serial:
        cmd += ["-s", _device_serial]
    cmd +=["forward", "--remove", f"tcp:{TUNNEL_PORT}"]
    subprocess.call(cmd, startupinfo=_si(),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


HEADER_SIZE   = 12
FLAG_CONFIG   = 1 << 63
FLAG_KEYFRAME = 1 << 62

def _do_handshake(sock: socket.socket):
    # Parsing the garbage scrcpy spits out when it connects.
    try:
        _recv_exactly(sock, 1)
        name = _recv_exactly(sock, 64)
        device_name = name.rstrip(b'\x00').decode('utf-8', errors='replace')
        print(f"[HANDSHAKE] Oh hey, it's {device_name}")
        meta = _recv_exactly(sock, 12)
        codec_id, w, h = struct.unpack(">III", meta)
        return codec_id, w, h
    except Exception as e:
        print(f"[HANDSHAKE] Botched: {e}")
        return None

def _do_audio_handshake(sock: socket.socket):
    try:
        meta = _recv_exactly(sock, 4)
        codec_id, = struct.unpack(">I", meta)
        return codec_id
    except Exception as e:
        print(f"[HANDSHAKE] Audio bit the dust: {e}")
        return None

def _video_worker(sock: socket.socket):
    # raw-dog the H.264 stream straight out of the socket into PyAV. 
    # Bypassing the demuxer entirely because fuck FFmpeg probing.
    global _latest_frame

    print("[VIDEO] Thread is awake.")
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 131072)

        result = _do_handshake(sock)
        if result is None:
            return
        _, width, height = result

        ctx, hw_name = _try_hw_decode_ctx(None)
        print(f"[VIDEO] Video context ready. HW={hw_name} {width}x{height}")

        config_data = b""
        frame_count = 0

        while not _stop_video.is_set():
            try:
                header = _recv_exactly(sock, HEADER_SIZE)
            except ConnectionError:
                print("[VIDEO] Stream dropped.")
                break

            # If you parse this header wrong, the stream desyncs by 1 byte and the universe collapses.
            pts_flags, size = struct.unpack(">QI", header)
            is_config   = bool(pts_flags & FLAG_CONFIG)
            is_keyframe = bool(pts_flags & FLAG_KEYFRAME)
            pts         = pts_flags & ~(3 << 62)

            payload = _recv_exactly(sock, size)

            if is_config:
                config_data = payload
                continue

            if is_keyframe and config_data:
                payload = config_data + payload

            try:
                packet = av.Packet(payload)
                packet.pts = pts
                for frame in ctx.decode(packet):
                    frame_count += 1
                    if _stop_video.is_set():
                        break

                    # Zero-copy YUV extraction. 
                    # I have no idea why the fuck this actually works
                    w, h = frame.width, frame.height
                    y = np.frombuffer(frame.planes[0], dtype=np.uint8).reshape(h,       frame.planes[0].line_size)[:h,  :w]
                    u = np.frombuffer(frame.planes[1], dtype=np.uint8).reshape(h // 2, frame.planes[1].line_size)[:h//2, :w//2]
                    v = np.frombuffer(frame.planes[2], dtype=np.uint8).reshape(h // 2, frame.planes[2].line_size)[:h//2, :w//2]
                    yuv = {"y": np.ascontiguousarray(y),
                           "u": np.ascontiguousarray(u),
                           "v": np.ascontiguousarray(v),
                           "w": w, "h": h}
                    with _frame_lock:
                        _latest_frame = yuv
            except Exception as e:
                # FFmpeg throws a tantrum on corrupted frames, just eat the exception and keep moving
                continue

    except Exception as e:
        print(f"[VIDEO] Complete and utter failure: {e}")
        import traceback; traceback.print_exc()
    finally:
        try:
            sock.close()
        except Exception:
            pass
        if state["scrcpy_status"] == "STREAMING":
            state["scrcpy_status"] = "READY"

def _try_hw_decode_ctx(unused) -> tuple:
    # CPU decoding. I benchmarked HW decode and copying it back from the GPU to Python is slower anyway.
    ctx = av.codec.CodecContext.create("h264", "r")
    ctx.flags  |= 0x00000002
    ctx.flags2 |= 0x00000001
    ctx.thread_count = 4
    ctx.thread_type  = 1
    ctx.open()
    return ctx, None


def _audio_worker(sock: socket.socket):
    global _pa, _pa_stream
    print("[AUDIO] Audio thread awake.")
    try:
        _do_audio_handshake(sock)
        _pa = pyaudio.PyAudio()

        ctx          = None
        resampler    = None
        config_bytes = b""
        rate         = AUDIO_RATE
        channels     = AUDIO_CHANNELS

        while not _stop_audio.is_set():
            try:
                header = _recv_exactly(sock, HEADER_SIZE)
            except ConnectionError:
                break

            pts_flags, size = struct.unpack(">QI", header)
            is_config = bool(pts_flags & FLAG_CONFIG)
            pts       = pts_flags & ~(3 << 62)
            payload   = _recv_exactly(sock, size)

            if is_config:
                # Parsing the bitwise monstrosity that is AudioSpecificConfig
                config_bytes = payload
                if len(payload) >= 2:
                    b0, b1 = payload[0], payload[1]
                    freq_idx = ((b0 & 0x07) << 1) | ((b1 & 0x80) >> 7)
                    freq_table =[96000,88200,64000,48000,44100,32000,
                                  24000,22050,16000,12000,11025,8000,7350]
                    if freq_idx < len(freq_table):
                        rate = freq_table[freq_idx]
                    channels = (b1 & 0x78) >> 3
                    if channels == 0:
                        channels = AUDIO_CHANNELS

                ctx = av.codec.CodecContext.create("aac", "r")
                ctx.sample_rate = rate
                ctx.layout      = "stereo" if channels >= 2 else "mono"
                ctx.open()

                _pa_stream = _pa.open(
                    format=pyaudio.paInt16,
                    channels=channels,
                    rate=rate,
                    output=True,
                    frames_per_buffer=AUDIO_CHUNK,
                )
                resampler = av.AudioResampler(
                    format="s16",
                    layout="stereo" if channels >= 2 else "mono",
                    rate=rate,
                )
                continue

            if ctx is None or _pa_stream is None:
                continue

            # I literally have to forge ADTS headers by hand because FFmpeg is too picky
            # to accept raw AAC frames without a container. Brainfucked.
            adts = _make_adts(payload, rate, channels)

            try:
                packet = av.Packet(adts)
                packet.pts = pts
                for frame in ctx.decode(packet):
                    if _stop_audio.is_set():
                        break
                    for rf in resampler.resample(frame):
                        _pa_stream.write(rf.to_ndarray().tobytes())
            except Exception:
                continue

    except Exception as e:
        print(f"[AUDIO] Well that didn't work: {e}")
        import traceback; traceback.print_exc()
    finally:
        try:
            if _pa_stream: _pa_stream.stop_stream(); _pa_stream.close()
            if _pa:        _pa.terminate()
            sock.close()
        except Exception:
            pass

def _make_adts(payload: bytes, rate: int, channels: int) -> bytes:
    # Bitwise operations designed to inflict psychological damage.
    freq_table =[96000,88200,64000,48000,44100,32000,
                  24000,22050,16000,12000,11025,8000,7350]
    try:
        freq_idx = freq_table.index(rate)
    except ValueError:
        freq_idx = 3

    chan_cfg    = min(channels, 7)
    frame_len   = len(payload) + 7
    obj_type    = 2 - 1

    b0 = 0xFF
    b1 = 0xF1
    b2 = ((obj_type & 0x03) << 6) | ((freq_idx & 0x0F) << 2) | ((chan_cfg & 0x04) >> 2)
    b3 = ((chan_cfg & 0x03) << 6) | ((frame_len & 0x1800) >> 11)
    b4 = (frame_len & 0x07F8) >> 3
    b5 = ((frame_len & 0x07) << 5) | 0x1F
    b6 = 0xFC

    return bytes([b0, b1, b2, b3, b4, b5, b6]) + payload

def _connect_sockets(timeout: float = 15.0):
    # Sleep and retry loop. Peak software engineering.
    deadline = time.time() + timeout
    video_sock = None
    audio_sock = None

    while time.time() < deadline:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect(("127.0.0.1", TUNNEL_PORT))
            s.settimeout(None)
            video_sock = s
            break
        except (ConnectionRefusedError, OSError):
            time.sleep(0.2)

    if video_sock is None:
        return None, None

    while time.time() < deadline:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect(("127.0.0.1", TUNNEL_PORT))
            s.settimeout(None)
            audio_sock = s
            break
        except (ConnectionRefusedError, OSError):
            time.sleep(0.2)

    if audio_sock is None:
        video_sock.close()
        return None, None

    return video_sock, audio_sock

def _launch_worker(device: str, res: str, max_fps: int = 60):
    global _server_proc, _video_thread, _audio_thread, _device_serial

    _stop_video.set()
    _stop_audio.set()
    if _server_proc:
        try: _server_proc.kill()
        except Exception: pass
        _server_proc = None
    _stop_video.clear()
    _stop_audio.clear()

    _device_serial = device
    _remove_tunnel()

    scid = random.randint(0, 0x7FFFFFFF)

    if not _push_server():
        state["scrcpy_status"] = "READY"
        return

    if not _setup_tunnel(scid):
        state["scrcpy_status"] = "READY"
        return

    proc = _start_server(scid, res, max_fps)
    if proc is None:
        state["scrcpy_status"] = "READY"
        return
    _server_proc = proc

    time.sleep(1.0)

    video_sock, audio_sock = _connect_sockets(timeout=15.0)
    if video_sock is None:
        proc.kill()
        _remove_tunnel()
        state["scrcpy_status"] = "READY"
        return

    _video_thread = threading.Thread(
        target=_video_worker, args=(video_sock,), daemon=True
    )
    _audio_thread = threading.Thread(
        target=_audio_worker, args=(audio_sock,), daemon=True
    )
    _video_thread.start()
    _audio_thread.start()

    state["scrcpy_status"] = "STREAMING"

def start_scrcpy():
    if not os.path.exists(server_path):
        state["scrcpy_status"] = "MISSING_EXE"
        return
    if state["devices"] == ["None"] or not state["devices"]:
        state["scrcpy_status"] = "NO_DEVICE"
        return
    device  = state["devices"][state["current_device_idx"]]
    res     = state["resolutions"][state["current_res_idx"]]
    fps     = state["max_fps"]
    state["scrcpy_status"] = "KILLING"
    threading.Thread(
        target=_launch_worker, args=(device, res, fps), daemon=True
    ).start()

def manage_scrcpy_state():
    status = state["scrcpy_status"]
    if status == "INIT":
        check_adb_devices()
    elif status == "STREAMING":
        if _server_proc and _server_proc.poll() is not None:
            _stop_video.set()
            _stop_audio.set()
            _remove_tunnel()
            state["scrcpy_status"] = "READY"

def apply_style():
    # Who needs a UI designer when you can hardcode hex values
    s = imgui.get_style()
    s.window_rounding = s.child_rounding = s.frame_rounding = 0.0
    s.grab_rounding   = s.tab_rounding   = 0.0
    s.window_padding  = (8.0, 8.0)
    s.item_spacing    = (8.0, 6.0)
    c = s.colors
    c[imgui.COLOR_WINDOW_BACKGROUND]        = (0.05, 0.05, 0.05, 1.0)
    c[imgui.COLOR_CHILD_BACKGROUND]         = (0.02, 0.02, 0.02, 1.0)
    c[imgui.COLOR_FRAME_BACKGROUND]         = (0.15, 0.15, 0.15, 1.0)
    c[imgui.COLOR_FRAME_BACKGROUND_HOVERED] = (0.25, 0.25, 0.25, 1.0)
    c[imgui.COLOR_FRAME_BACKGROUND_ACTIVE]  = (0.30, 0.30, 0.30, 1.0)
    c[imgui.COLOR_BORDER]                   = (0.30, 0.30, 0.30, 1.0)
    c[imgui.COLOR_TEXT]                     = (0.95, 0.95, 0.95, 1.0)
    c[imgui.COLOR_HEADER]                   = (0.15, 0.15, 0.15, 1.0)
    c[imgui.COLOR_HEADER_HOVERED]           = (0.20, 0.20, 0.20, 1.0)
    c[imgui.COLOR_HEADER_ACTIVE]            = (0.25, 0.25, 0.25, 1.0)
    c[imgui.COLOR_TAB]                      = (0.10, 0.10, 0.10, 1.0)
    c[imgui.COLOR_TAB_HOVERED]             = (0.20, 0.20, 0.20, 1.0)
    c[imgui.COLOR_TAB_ACTIVE]              = (0.15, 0.15, 0.15, 1.0)
    c[imgui.COLOR_CHECK_MARK]              = (1.00, 1.00, 1.00, 1.0)


def render_ui():
    manage_scrcpy_state()

    with _frame_lock:
        yuv = _latest_frame
    if yuv is not None and _yuv_prog is not None:
        _upload_yuv_and_render(yuv)

    io = imgui.get_io()
    imgui.set_next_window_position(0, 0)
    imgui.set_next_window_size(io.display_size.x, io.display_size.y)
    flags = (
        imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_TITLE_BAR  |
        imgui.WINDOW_NO_RESIZE   | imgui.WINDOW_NO_MOVE       |
        imgui.WINDOW_NO_BRING_TO_FRONT_ON_FOCUS
    )
    imgui.push_style_var(imgui.STYLE_WINDOW_PADDING, (0, 0))
    imgui.begin("MainWorkspace", flags=flags)
    imgui.pop_style_var()

    avail_w  = imgui.get_content_region_available().x
    avail_h  = imgui.get_content_region_available().y
    left_w   = 320.0
    right_w  = 300.0
    center_w = avail_w - left_w - right_w

    imgui.begin_child("LeftPanel", left_w, avail_h, border=True)
    if imgui.begin_tab_bar("SettingsTabs"):
        if imgui.begin_tab_item("SCRCPY")[0]:
            imgui.spacing()
            imgui.text("Target Device")
            status = state["scrcpy_status"]
            busy   = status in ("STARTING_ADB", "KILLING")

            imgui.push_item_width(180.0)
            _, state["current_device_idx"] = imgui.combo(
                "##device", state["current_device_idx"], state["devices"]
            )
            imgui.pop_item_width()
            imgui.same_line()
            if busy:
                imgui.push_style_color(imgui.COLOR_BUTTON,         0.2, 0.2, 0.2, 1.0)
                imgui.push_style_color(imgui.COLOR_BUTTON_HOVERED, 0.2, 0.2, 0.2, 1.0)
                imgui.push_style_color(imgui.COLOR_BUTTON_ACTIVE,  0.2, 0.2, 0.2, 1.0)
            if imgui.button("Refresh") and not busy:
                check_adb_devices()
            if busy:
                imgui.pop_style_color(3)

            imgui.spacing()
            imgui.text("Resolution Limit")
            imgui.push_item_width(180.0)
            _, state["current_res_idx"] = imgui.combo(
                "##res", state["current_res_idx"], state["resolutions"]
            )
            imgui.pop_item_width()

            imgui.spacing()
            imgui.text("Max FPS")
            imgui.push_item_width(80.0)
            changed, new_fps = imgui.input_int("##fps", state["max_fps"])
            if changed:
                state["max_fps"] = max(1, min(120, new_fps))
            imgui.pop_item_width()
            imgui.spacing(); imgui.spacing()

            if busy:
                imgui.push_style_color(imgui.COLOR_BUTTON,         0.2, 0.2, 0.2, 1.0)
                imgui.push_style_color(imgui.COLOR_BUTTON_HOVERED, 0.2, 0.2, 0.2, 1.0)
                imgui.push_style_color(imgui.COLOR_BUTTON_ACTIVE,  0.2, 0.2, 0.2, 1.0)
            if imgui.button("Connect / Restart Scrcpy", width=250.0) and not busy:
                start_scrcpy()
            if busy:
                imgui.pop_style_color(3)

            imgui.spacing()
            hint_map = {
                "INIT":         ("Initialising...",          0.7, 0.7, 0.0),
                "STARTING_ADB": ("Scanning ADB devices...", 0.7, 0.7, 0.7),
                "KILLING":      ("Stopping old session...", 0.7, 0.7, 0.7),
                "NO_DEVICE":    ("No device found.",        1.0, 0.3, 0.3),
                "MISSING_EXE":  ("scrcpy-server missing!",  1.0, 0.3, 0.3),
                "READY":        ("Ready — press Connect.",  0.4, 0.8, 0.4),
                "STREAMING":    ("Streaming.",              0.2, 1.0, 0.2),
            }
            hint, r, g, b = hint_map.get(status, (status, 0.7, 0.7, 0.7))
            imgui.text_colored(hint, r, g, b)
            imgui.end_tab_item()

        if imgui.begin_tab_item("AutoSekai")[0]:
            imgui.spacing()
            imgui.text("Visual")
            _, state["show_touch"] = imgui.checkbox(
                "Show AutoSekai Touch", state["show_touch"]
            )
            imgui.spacing(); imgui.spacing()
            imgui.text("AutoSekai")
            imgui.push_item_width(60.0)
            _, state["delay"] = imgui.input_float(
                "ms Delay after Pause", state["delay"], format="%.2f"
            )
            imgui.pop_item_width()
            _, state["ai_chart"] = imgui.checkbox(
                "AI Chart text detection", state["ai_chart"]
            )
            imgui.end_tab_item()

        if imgui.begin_tab_item("AI")[0]:
            imgui.text("AI Configuration coming soon...")
            imgui.end_tab_item()

        imgui.end_tab_bar()
    imgui.end_child()
    imgui.same_line(spacing=0)

    imgui.begin_child("CenterPanel", center_w, avail_h, border=True)
    imgui.spacing()
    imgui.text("Current .SUS File")
    imgui.same_line()
    imgui.push_item_width(center_w - 250.0)
    _, state["current_file_idx"] = imgui.combo(
        "##susfile", state["current_file_idx"], state["files"]
    )
    imgui.pop_item_width()
    imgui.same_line()
    if imgui.button("Browse..."):
        # Spawning an entire tk instance just for a file dialog. fuck me.
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
        fp = filedialog.askopenfilename(
            title="Select .SUS Chart",
            filetypes=(("Project Sekai Chart", "*.sus"), ("All Files", "*.*")),
        )
        root.destroy()
        if fp:
            if state["files"] == ["None"]: state["files"] = []
            if fp not in state["files"]:   state["files"].append(fp)
            state["current_file_idx"] = state["files"].index(fp)

    imgui.text("Status : "); imgui.same_line()
    imgui.text_colored("Not Playing", 1.0, 0.2, 0.2)
    imgui.spacing()

    is_open, _ = imgui.collapsing_header(
        "SCRCPY", flags=imgui.TREE_NODE_DEFAULT_OPEN
    )
    if is_open:
        video_h = imgui.get_content_region_available().y - 200
        imgui.begin_child("VideoRegion", 0, video_h, border=True)
        box_w, box_h = imgui.get_window_size()
        status = state["scrcpy_status"]

        if status == "STREAMING" and _tex_id is not None and _tex_w > 0:
            aspect = _tex_w / max(_tex_h, 1)
            draw_w = box_w
            draw_h = box_w / aspect
            if draw_h > box_h:
                draw_h = box_h
                draw_w = box_h * aspect
            imgui.set_cursor_pos(((box_w - draw_w) / 2, (box_h - draw_h) / 2))
            imgui.image(_tex_id, draw_w, draw_h)
        else:
            msg_map = {
                "STARTING_ADB": ("Scanning ADB devices...",       0.7, 0.7, 0.7),
                "KILLING":      ("Stopping previous session...",  0.7, 0.7, 0.7),
                "STREAMING":    ("Waiting for first frame...",    0.7, 0.7, 0.7),
                "READY":        ("Device ready — press Connect.", 0.4, 0.8, 0.4),
                "NO_DEVICE":    ("No device connected.",          1.0, 0.3, 0.3),
                "MISSING_EXE":  ("scrcpy-server not found!",      1.0, 0.3, 0.3),
            }
            if status in msg_map:
                msg, r, g, b = msg_map[status]
                ts = imgui.calc_text_size(msg)
                imgui.set_cursor_pos(
                    ((box_w - ts.x) / 2, (box_h - ts.y) / 2)
                )
                imgui.text_colored(msg, r, g, b)
        imgui.end_child()

    if imgui.collapsing_header(
        "SCRCPY Log", flags=imgui.TREE_NODE_DEFAULT_OPEN
    )[0]:
        imgui.begin_child("ScrcpyLogRegion", 0, 0, border=True)
        imgui.text_colored(
            f"> Status: {state['scrcpy_status']}", 0.6, 0.6, 0.6
        )
        if _server_proc:
            imgui.text_colored(
                f"> Server PID: {_server_proc.pid}", 0.4, 0.4, 0.6
            )
        if _tex_w:
            imgui.text_colored(
                f"> Frame: {_tex_w}x{_tex_h}", 0.4, 0.4, 0.6
            )
        imgui.end_child()

    imgui.end_child()
    imgui.same_line(spacing=0)

    imgui.begin_child("RightPanel", right_w, avail_h, border=True)
    if imgui.collapsing_header(
        "AutoSekai & AI Log", flags=imgui.TREE_NODE_DEFAULT_OPEN
    )[0]:
        imgui.begin_child("AutoSekaiLogRegion", 0, 0, border=True)
        imgui.text_colored("> AutoSekai UI Initialized.", 0.4, 0.8, 0.4)
        imgui.text_colored("> Ready.", 0.4, 0.8, 0.4)
        imgui.end_child()
    imgui.end_child()
    imgui.end()

def main():
    _fetch_scrcpy_dependencies()
    _remove_tunnel()

    if not glfw.init():
        sys.exit(1)

    glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
    glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
    glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)

    window = glfw.create_window(1440, 810, "SenyxSekai0", None, None)
    if not window:
        glfw.terminate()
        sys.exit(1)

    glfw.make_context_current(window)
    glfw.swap_interval(1)

    imgui.create_context()
    io = imgui.get_io()
    io.fonts.clear()
    
    # Japanese font loading. doesnt work anyway.
    font_path = r"C:\Windows\Fonts\meiryo.ttc"
    if os.path.exists(font_path):
        try:
            io.fonts.add_font_from_file_ttf(
                font_path, 16.0,
                glyph_ranges=io.fonts.get_glyph_ranges_japanese(),
            )
        except Exception:
            io.fonts.add_font_default()
    else:
        io.fonts.add_font_default()

    impl = GlfwRenderer(window)
    apply_style()
    _init_yuv_pipeline()

    # if you're tempreture rise, blame this.
    while not glfw.window_should_close(window):
        glfw.poll_events()
        impl.process_inputs()
        imgui.new_frame()
        render_ui()
        gl.glClearColor(0.0, 0.0, 0.0, 1)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        imgui.render()
        impl.render(imgui.get_draw_data())
        glfw.swap_buffers(window)

    # Nuke everything on exit so ADB doesn't leave orphaned tunnels
    _stop_video.set()
    _stop_audio.set()
    if _server_proc:
        try: _server_proc.kill()
        except Exception: pass
    _remove_tunnel()
    impl.shutdown()
    glfw.terminate()


if __name__ == "__main__":
    main()
