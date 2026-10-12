"""Runs inside the Blender GUI. Builds a starter scene, then takes bpy snippets over HTTP.

POST a Python snippet to / and it runs on Blender's main thread against the live scene.
The response is the snippet's stdout, or its traceback. GET / returns the viewport
renderer, which also tells the caller the scene is ready.
"""

import contextlib
import io
import math
import queue
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bpy
import gpu

jobs: queue.Queue = queue.Queue()


def build_scene():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj)
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_EEVEE_NEXT"
    bpy.ops.mesh.primitive_plane_add(size=20)
    bpy.context.object.name = "Ground"
    bpy.ops.mesh.primitive_monkey_add(location=(0, 0, 1.2))
    monkey = bpy.context.object
    monkey.modifiers.new("Subdivision", "SUBSURF").levels = 2
    bpy.ops.object.shade_smooth()
    orange = bpy.data.materials.new("Orange")
    orange.use_nodes = True
    orange.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (1, 0.35, 0.05, 1)
    monkey.data.materials.append(orange)
    bpy.ops.object.light_add(type="SUN", location=(4, -4, 8))
    bpy.context.object.data.energy = 4
    bpy.ops.object.camera_add(location=(0, -9, 4), rotation=(math.radians(72), 0, 0))
    scene.camera = bpy.context.object
    scene.world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.05, 0.06, 0.08, 1)


def look_through_camera():
    # Rendered shading, so lights and materials show live in the viewport.
    window = bpy.context.window_manager.windows[0]
    area = next(a for a in window.screen.areas if a.type == "VIEW_3D")
    area.spaces[0].shading.type = "RENDERED"
    area.spaces[0].region_3d.view_perspective = "CAMERA"
    region = next(r for r in area.regions if r.type == "WINDOW")
    with bpy.context.temp_override(window=window, area=area, region=region):
        bpy.ops.view3d.view_center_camera()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.reply(gpu.platform.renderer_get())

    def do_POST(self):
        code = self.rfile.read(int(self.headers["Content-Length"])).decode()
        done, out = threading.Event(), {}
        jobs.put((code, out, done))  # bpy is not thread safe; the timer below runs it
        done.wait()
        self.reply(out["text"])

    def reply(self, text: str):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(text.encode())

    def log_message(self, *args):
        pass


def run_jobs():
    while not jobs.empty():
        code, out, done = jobs.get()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                exec(code, {"bpy": bpy, "math": math})
            except Exception:
                traceback.print_exc(file=buf)
        out["text"] = buf.getvalue()
        done.set()
    return 0.1  # seconds until the next call


def start():
    build_scene()
    look_through_camera()
    server = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    bpy.app.timers.register(run_jobs)


# The window exists only after startup, so build the scene from a timer.
bpy.app.timers.register(start, first_interval=1.0)
