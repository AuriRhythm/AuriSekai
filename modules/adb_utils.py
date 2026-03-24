import os
import sys
import subprocess
import urllib.request
import zipfile
import shutil
import threading

from modules import core_state as st

def _fetch_scrcpy_dependencies():
    if os.path.exists(st.adb_path) and os.path.exists(st.server_path):
        return

    print("[BOOT] Missing scrcpy binaries. Initiating payload delivery...")
    os.makedirs(os.path.dirname(st.adb_path), exist_ok=True)
    
    zip_url = f"https://github.com/Genymobile/scrcpy/releases/download/v{st.SERVER_VERSION}/scrcpy-win64-v{st.SERVER_VERSION}.zip"
    zip_path = os.path.join(st.base_dir, "scrcpy_temp.zip")

    try:
        print(f"[BOOT] Downloading scrcpy v{st.SERVER_VERSION} (this might take a sec)...")
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
                    target_path = os.path.join(st.base_dir, "scrcpy", filename)
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
    cmd =[st.adb_path if os.path.exists(st.adb_path) else "adb"]
    if st._device_serial:
        cmd +=["-s", st._device_serial]
    cmd += list(args)
    if capture:
        return subprocess.check_output(cmd, text=True,
                                       startupinfo=_si(),
                                       stderr=subprocess.DEVNULL)
    else:
        return subprocess.call(cmd, startupinfo=_si(),
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)

def _adb_worker():
    # Scraping adb stdout because Google refuses to provide a decent machine-readable format
    adb_bin = st.adb_path if os.path.exists(st.adb_path) else "adb"
    try:
        out   = subprocess.check_output([adb_bin, "devices"],
                                        text=True, startupinfo=_si())
        lines = out.strip().split("\n")[1:]
        devices = [ln.split()[0] for ln in lines
                   if "device" in ln and "offline" not in ln]
        if devices:
            st.state["devices"]            = devices
            st.state["current_device_idx"] = 0
            st._device_serial              = devices[0]
            st.state["scrcpy_status"]      = "READY"
        else:
            st.state["devices"]            = ["None"]
            st.state["current_device_idx"] = 0
            st.state["scrcpy_status"]      = "NO_DEVICE"
    except Exception as e:
        print(f"[ADB] Dumpstered: {e}")
        st.state["devices"]       = ["None"]
        st.state["scrcpy_status"] = "NO_DEVICE"

def check_adb_devices():
    st.state["scrcpy_status"] = "STARTING_ADB"
    threading.Thread(target=_adb_worker, daemon=True).start()

def _push_server():
    if not os.path.exists(st.server_path):
        print(f"[SERVER] Bruh, where is the jar? Missing at {st.server_path}")
        return False
    try:
        _adb("push", st.server_path, st.DEVICE_SERVER_PATH, capture=False)
        print(f"[SERVER] Yeeted {st.server_path} -> {st.DEVICE_SERVER_PATH}")
        return True
    except Exception as e:
        print(f"[SERVER] ADB push failed: {e}")
        return False

def _start_server(scid: int, res: str, max_fps: int = 60, encoder: str = "Auto") -> subprocess.Popen | None:
    # The arcane incantation to make app_process execute a java class directly on the phone.
    # turn OFF control and ON raw_stream=false so it actually get packet headers.
    adb_bin = st.adb_path if os.path.exists(st.adb_path) else "adb"

    max_size = res if res != "Max" else "0"
    scid_hex = format(scid, "08x")

    cmd_args = [
        f"CLASSPATH={st.DEVICE_SERVER_PATH}",
        "app_process",
        "/",
        "com.genymobile.scrcpy.Server",
        st.SERVER_VERSION,
        f"scid={scid_hex}",
        "tunnel_forward=true",
        f"max_size={max_size}",
        f"max_fps={max_fps}",
        "video=true",
        "audio=false",
        "control=false",
        "raw_stream=false", 
        "send_dummy_byte=true",
        "send_device_meta=true",
        "send_frame_meta=true",
        "video_codec=h264",
        "cleanup=true",
    ]
    if encoder != "Auto":
        cmd_args.insert(-1, f"video_encoder={encoder}")
    server_args = " ".join(cmd_args)

    cmd =[adb_bin]
    if st._device_serial:
        cmd += ["-s", st._device_serial]
    cmd += ["shell", server_args]

    try:
        proc = subprocess.Popen(
            cmd,
            startupinfo=_si(),
        )
        print(f"[SERVER] Sent the jar to the shadow realm. PID={proc.pid}")
        return proc
    except Exception as e:
        print(f"[SERVER] App_process completely shit the bed: {e}")
        return None

def _setup_tunnel(scid: int) -> bool:
    adb_bin = st.adb_path if os.path.exists(st.adb_path) else "adb"
    abstract = f"localabstract:scrcpy_{format(scid, '08x')}"
    cmd = [adb_bin]
    if st._device_serial:
        cmd += ["-s", st._device_serial]
    cmd +=["forward", f"tcp:{st.TUNNEL_PORT}", abstract]
    try:
        subprocess.check_call(cmd, startupinfo=_si(),
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        print(f"[TUNNEL] Port forward failed. Are we already bound?: {e}")
        return False

def _remove_tunnel():
    adb_bin = st.adb_path if os.path.exists(st.adb_path) else "adb"
    cmd = [adb_bin]
    if st._device_serial:
        cmd += ["-s", st._device_serial]
    cmd +=["forward", "--remove", f"tcp:{st.TUNNEL_PORT}"]
    subprocess.call(cmd, startupinfo=_si(),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
