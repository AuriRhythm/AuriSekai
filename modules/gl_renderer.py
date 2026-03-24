import OpenGL.GL as gl
import numpy as np
import imgui

from modules import core_state as st

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
_fbo         = None

def _compile_shader(src: str, kind: int) -> int:
    shader = gl.glCreateShader(kind)
    gl.glShaderSource(shader, src)
    gl.glCompileShader(shader)
    if not gl.glGetShaderiv(shader, gl.GL_COMPILE_STATUS):
        raise RuntimeError(f"Shader refused to compile: {gl.glGetShaderInfoLog(shader)}")
    return shader

def _init_yuv_pipeline():
    # I love writing 60 lines of OpenGL boilerplate just to draw a fucking square
    global _yuv_prog, _yuv_vao, _tex_y, _tex_u, _tex_v, _fbo

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

    st._tex_id = make_tex()
    _fbo    = gl.glGenFramebuffers(1)
    print("[GL] Shader pipeline actually survived initialization.")

def _upload_yuv_and_render(yuv: dict):
    # Ramming raw memory buffers straight into the GPU. 
    global _yuv_w, _yuv_h

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
        gl.glBindTexture(gl.GL_TEXTURE_2D, st._tex_id)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGB, w, h, 0,
                        gl.GL_RGB, gl.GL_UNSIGNED_BYTE, None)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, _fbo)
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                  gl.GL_TEXTURE_2D, st._tex_id, 0)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        _yuv_w, _yuv_h = w, h
        st._tex_w, st._tex_h = w, h

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
