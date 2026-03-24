import os
import threading

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
    "encoders": ["Auto", "c2.android.avc.encoder", "OMX.qcom.video.encoder.avc", "OMX.google.h264.encoder", "c2.exynos.h264.encoder"],
    "current_encoder_idx": 0,
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
