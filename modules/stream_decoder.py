import socket
import struct
import time
import random
import threading

import av
import pyaudio
import numpy as np

from modules import core_state as st
from modules import adb_utils as adb

HEADER_SIZE   = 12
FLAG_CONFIG   = 1 << 63
FLAG_KEYFRAME = 1 << 62

def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    # If the socket dies halfway through a payload, Its fucked anyway.
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Socket gave up on life")
        buf += chunk
    return buf

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

        while not st._stop_video.is_set():
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
                config_data += payload
                continue

            if is_keyframe and config_data:
                payload = config_data + payload

            try:
                packet = av.Packet(payload)
                packet.pts = pts
                for frame in ctx.decode(packet):
                    frame_count += 1
                    if st._stop_video.is_set():
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
                    with st._frame_lock:
                        st._latest_frame = yuv
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
        if st.state["scrcpy_status"] == "STREAMING":
            st.state["scrcpy_status"] = "READY"

def _try_hw_decode_ctx(unused) -> tuple:
    ctx = av.codec.CodecContext.create("h264", "r")
    ctx.thread_count = 1
    return ctx, None


def _audio_worker(sock: socket.socket):
    print("[AUDIO] Audio thread awake.")
    try:
        _do_audio_handshake(sock)
        st._pa = pyaudio.PyAudio()

        ctx          = None
        resampler    = None
        config_bytes = b""
        rate         = st.AUDIO_RATE
        channels     = st.AUDIO_CHANNELS

        while not st._stop_audio.is_set():
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
                        channels = st.AUDIO_CHANNELS

                ctx = av.codec.CodecContext.create("aac", "r")
                ctx.sample_rate = rate
                ctx.layout      = "stereo" if channels >= 2 else "mono"
                ctx.open()

                st._pa_stream = st._pa.open(
                    format=pyaudio.paInt16,
                    channels=channels,
                    rate=rate,
                    output=True,
                    frames_per_buffer=st.AUDIO_CHUNK,
                )
                resampler = av.AudioResampler(
                    format="s16",
                    layout="stereo" if channels >= 2 else "mono",
                    rate=rate,
                )   
                continue

            if ctx is None or st._pa_stream is None:
                continue

            # I literally have to forge ADTS headers by hand because FFmpeg is too picky
            # to accept raw AAC frames without a container. Brainfucked.
            adts = _make_adts(payload, rate, channels)

            try:
                packet = av.Packet(adts)
                packet.pts = pts
                for frame in ctx.decode(packet):
                    if st._stop_audio.is_set():
                        break
                    for rf in resampler.resample(frame):
                        st._pa_stream.write(rf.to_ndarray().tobytes())
            except Exception:
                continue

    except Exception as e:
        print(f"[AUDIO] Well that didn't work: {e}")
        import traceback; traceback.print_exc()
    finally:
        try:
            if st._pa_stream: st._pa_stream.stop_stream(); st._pa_stream.close()
            if st._pa:        st._pa.terminate()
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
            s.connect(("127.0.0.1", st.TUNNEL_PORT))
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
            s.connect(("127.0.0.1", st.TUNNEL_PORT))
            s.settimeout(None)
            audio_sock = s
            break
        except (ConnectionRefusedError, OSError):
            time.sleep(0.2)

    if audio_sock is None:
        video_sock.close()
        return None, None
    return video_sock, audio_sock

def _launch_worker(device: str, res: str, max_fps: int = 60, encoder: str = "Auto"):
    st._stop_video.set()
    st._stop_audio.set()
    if st._server_proc:
        try: st._server_proc.kill()
        except Exception: pass
        st._server_proc = None
    st._stop_video.clear()
    st._stop_audio.clear()

    st._device_serial = device
    adb._remove_tunnel()

    scid = random.randint(0, 0x7FFFFFFF)

    if not adb._push_server():
        st.state["scrcpy_status"] = "READY"
        return

    if not adb._setup_tunnel(scid):
        st.state["scrcpy_status"] = "READY"
        return

    proc = adb._start_server(scid, res, max_fps, encoder)
    if proc is None:
        st.state["scrcpy_status"] = "READY"
        return
    st._server_proc = proc

    time.sleep(1.0)

    video_sock, audio_sock = _connect_sockets(timeout=15.0)
    if video_sock is None:
        proc.kill()
        adb._remove_tunnel()
        st.state["scrcpy_status"] = "READY"
        return

    st._video_thread = threading.Thread(
        target=_video_worker, args=(video_sock,), daemon=True
    )
    st._audio_thread = threading.Thread(
        target=_audio_worker, args=(audio_sock,), daemon=True
    )
    st._video_thread.start()
    st._audio_thread.start()

    st.state["scrcpy_status"] = "STREAMING"

def start_scrcpy():
    import os
    if not os.path.exists(st.server_path):
        st.state["scrcpy_status"] = "MISSING_EXE"
        return
    if st.state["devices"] == ["None"] or not st.state["devices"]:
        st.state["scrcpy_status"] = "NO_DEVICE"
        return
    device  = st.state["devices"][st.state["current_device_idx"]]
    res     = st.state["resolutions"][st.state["current_res_idx"]]
    fps     = st.state["max_fps"]
    enc     = st.state["encoders"][st.state["current_encoder_idx"]]
    st.state["scrcpy_status"] = "KILLING"
    threading.Thread(
        target=_launch_worker, args=(device, res, fps, enc), daemon=True
    ).start()

def manage_scrcpy_state():
    status = st.state["scrcpy_status"]
    if status == "INIT":
        adb.check_adb_devices()
    elif status == "STREAMING":
        if st._server_proc and st._server_proc.poll() is not None:
            st._stop_video.set()
            st._stop_audio.set()
            adb._remove_tunnel()
            st.state["scrcpy_status"] = "READY"