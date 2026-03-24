import imgui
from modules import core_state as st
from modules import adb_utils as adb
from modules import gl_renderer as glr
from modules import stream_decoder as stream

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
    stream.manage_scrcpy_state()

    with st._frame_lock:
        yuv = st._latest_frame
    if yuv is not None and glr._yuv_prog is not None:
        glr._upload_yuv_and_render(yuv)

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
            status = st.state["scrcpy_status"]
            busy   = status in ("STARTING_ADB", "KILLING")

            imgui.push_item_width(180.0)
            _, st.state["current_device_idx"] = imgui.combo(
                "##device", st.state["current_device_idx"], st.state["devices"]
            )
            imgui.pop_item_width()
            imgui.same_line()
            if busy:
                imgui.push_style_color(imgui.COLOR_BUTTON,         0.2, 0.2, 0.2, 1.0)
                imgui.push_style_color(imgui.COLOR_BUTTON_HOVERED, 0.2, 0.2, 0.2, 1.0)
                imgui.push_style_color(imgui.COLOR_BUTTON_ACTIVE,  0.2, 0.2, 0.2, 1.0)
            if imgui.button("Refresh") and not busy:
                adb.check_adb_devices()
            if busy:
                imgui.pop_style_color(3)

            imgui.spacing()
            imgui.text("Resolution Limit")
            imgui.push_item_width(180.0)
            _, st.state["current_res_idx"] = imgui.combo(
                "##res", st.state["current_res_idx"], st.state["resolutions"]
            )
            imgui.pop_item_width()

            imgui.spacing()
            imgui.text("Video Encoder")
            imgui.push_item_width(180.0)
            _, st.state["current_encoder_idx"] = imgui.combo(
                "##enc", st.state["current_encoder_idx"], st.state["encoders"]
            )
            imgui.pop_item_width()

            imgui.spacing()
            imgui.text("Max FPS")
            imgui.push_item_width(80.0)
            changed, new_fps = imgui.input_int("##fps", st.state["max_fps"])
            if changed:
                st.state["max_fps"] = max(1, min(120, new_fps))
            imgui.pop_item_width()
            imgui.spacing(); imgui.spacing()

            if busy:
                imgui.push_style_color(imgui.COLOR_BUTTON,         0.2, 0.2, 0.2, 1.0)
                imgui.push_style_color(imgui.COLOR_BUTTON_HOVERED, 0.2, 0.2, 0.2, 1.0)
                imgui.push_style_color(imgui.COLOR_BUTTON_ACTIVE,  0.2, 0.2, 0.2, 1.0)
            if imgui.button("Connect / Restart Scrcpy", width=250.0) and not busy:
                stream.start_scrcpy()
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
            _, st.state["show_touch"] = imgui.checkbox(
                "Show AutoSekai Touch", st.state["show_touch"]
            )
            imgui.spacing(); imgui.spacing()
            imgui.text("AutoSekai")
            imgui.push_item_width(60.0)
            _, st.state["delay"] = imgui.input_float(
                "ms Delay after Pause", st.state["delay"], format="%.2f"
            )
            imgui.pop_item_width()
            _, st.state["ai_chart"] = imgui.checkbox(
                "AI Chart text detection", st.state["ai_chart"]
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
    _, st.state["current_file_idx"] = imgui.combo(
        "##susfile", st.state["current_file_idx"], st.state["files"]
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
            if st.state["files"] == ["None"]: st.state["files"] = []
            if fp not in st.state["files"]:   st.state["files"].append(fp)
            st.state["current_file_idx"] = st.state["files"].index(fp)

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
        status = st.state["scrcpy_status"]

        if status == "STREAMING" and st._tex_id is not None and st._tex_w > 0:
            aspect = st._tex_w / max(st._tex_h, 1)
            draw_w = box_w
            draw_h = box_w / aspect
            if draw_h > box_h:
                draw_h = box_h
                draw_w = box_h * aspect
            imgui.set_cursor_pos(((box_w - draw_w) / 2, (box_h - draw_h) / 2))
            imgui.image(st._tex_id, draw_w, draw_h)
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
            f"> Status: {st.state['scrcpy_status']}", 0.6, 0.6, 0.6
        )
        if st._server_proc:
            imgui.text_colored(
                f"> Server PID: {st._server_proc.pid}", 0.4, 0.4, 0.6
            )
        if st._tex_w:
            imgui.text_colored(
                f"> Frame: {st._tex_w}x{st._tex_h}", 0.4, 0.4, 0.6
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
