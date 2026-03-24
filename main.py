# Just pip install the requirements and pray the PyAV binaries don't segfault.
import glfw
import OpenGL.GL as gl
import imgui
from imgui.integrations.glfw import GlfwRenderer

import sys
import os

from modules import core_state as st
from modules import adb_utils as adb
from modules import gl_renderer as glr
from modules import ui_renderer as ui

def main():
    adb._fetch_scrcpy_dependencies()
    adb._remove_tunnel()

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
    ui.apply_style()
    glr._init_yuv_pipeline()

    # if you're tempreture rise, blame this.
    while not glfw.window_should_close(window):
        glfw.poll_events()
        impl.process_inputs()
        imgui.new_frame()
        ui.render_ui()
        gl.glClearColor(0.0, 0.0, 0.0, 1)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        imgui.render()
        impl.render(imgui.get_draw_data())
        glfw.swap_buffers(window)

    # Nuke everything on exit so ADB doesn't leave orphaned tunnels
    st._stop_video.set()
    st._stop_audio.set()
    if st._server_proc:
        try: st._server_proc.kill()
        except Exception: pass
    adb._remove_tunnel()
    impl.shutdown()
    glfw.terminate()

if __name__ == "__main__":
    main()