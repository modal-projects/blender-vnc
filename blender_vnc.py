"""Blender's GUI in a Modal Sandbox: GPU-rendered viewport, watched in the browser.

    modal run blender_vnc.py

Prints a noVNC URL and a URL that runs bpy code. Open the first, watch the script edit
the scene, then click around.
"""

import time
import urllib.error
import urllib.request

import modal

app = modal.App("blender-vnc")

BLENDER = "blender-4.5.14-linux-x64"
BLENDER_URL = f"https://download.blender.org/release/Blender4.5/{BLENDER}.tar.xz"
BLENDER_MIRROR = f"https://mirrors.ocf.berkeley.edu/blender/release/Blender4.5/{BLENDER}.tar.xz"
VIRTUALGL = "https://github.com/VirtualGL/virtualgl/releases/download/3.1.3/virtualgl_3.1.3_amd64.deb"
TURBOVNC = "https://github.com/TurboVNC/turbovnc/releases/download/3.3.1/turbovnc_3.3.1_amd64.deb"

image = (
    modal.Image.debian_slim()
    .apt_install("curl", "xz-utils")
    # Blender's GUI libraries
    .apt_install("libxi6", "libxxf86vm1", "libxfixes3", "libxrender1", "libsm6", "libice6", "libxkbcommon0")
    .apt_install(
        "libxkbcommon-x11-0", "libegl1", "libgl1", "libvulkan1", "libwayland-client0", "libwayland-egl1"
    )
    .apt_install("libwayland-cursor0")
    # X server, fonts and noVNC
    .apt_install(
        "xfonts-base", "x11-xkb-utils", "xkb-data", "xauth", "libgl1-mesa-dri", "novnc", "websockify"
    )
    .run_commands(
        # download.blender.org sometimes refuses the image builder, so fall back to a mirror.
        f"curl -fsSLo /tmp/blender.tar.xz {BLENDER_URL} || curl -fsSLo /tmp/blender.tar.xz {BLENDER_MIRROR}",
        f"tar -xJf /tmp/blender.tar.xz -C /opt && rm /tmp/blender.tar.xz && ln -s /opt/{BLENDER}/blender /usr/local/bin/blender",
        f"curl -fsSLo /tmp/vgl.deb {VIRTUALGL} && apt-get install -y /tmp/vgl.deb",
        f"curl -fsSLo /tmp/tvnc.deb {TURBOVNC} && apt-get install -y /tmp/tvnc.deb",
        # Skip the first-run splash, so the viewer lands in the scene.
        "blender --background --python-expr"
        " 'import bpy; bpy.context.preferences.view.show_splash = False; bpy.ops.wm.save_userpref()'",
    )
    .add_local_file("blender_live.py", "/root/blender_live.py")
)

# TurboVNC's Xvnc is the X server and the VNC server in one. websockify puts it behind
# a websocket for noVNC. VirtualGL (vglrun) sends Blender's OpenGL to the GPU through
# EGL and copies finished frames into the X server, which has no GPU of its own.
DESKTOP = """
/opt/TurboVNC/bin/Xvnc :1 -geometry 1600x900 -depth 24 -rfbport 5900 -securitytypes none \
    -alwaysshared -fp /usr/share/fonts/X11/misc -dridir /usr/lib/x86_64-linux-gnu/dri -deferupdate 1 &
websockify --web /usr/share/novnc 6080 localhost:5900 &
sleep 2
DISPLAY=:1 vglrun -d egl blender --python /root/blender_live.py > /tmp/blender.log 2>&1
"""

# What the script does to the scene while you watch. Any bpy code works here.
EDITS = [
    (
        "add a blue cube",
        "bpy.ops.mesh.primitive_cube_add(size=1.5, location=(2.5, 0, 0.75))\n"
        "blue = bpy.data.materials.new('Blue'); blue.use_nodes = True\n"
        "blue.node_tree.nodes['Principled BSDF'].inputs['Base Color'].default_value = (0.1, 0.3, 1, 1)\n"
        "bpy.context.object.data.materials.append(blue)",
    ),
    (
        "make the monkey gold",
        "bsdf = bpy.data.materials['Orange'].node_tree.nodes['Principled BSDF']\n"
        "bsdf.inputs['Base Color'].default_value = (1, 0.75, 0.2, 1)\n"
        "bsdf.inputs['Metallic'].default_value = 1\n"
        "bsdf.inputs['Roughness'].default_value = 0.25",
    ),
    (
        "add a purple point light",
        "bpy.ops.object.light_add(type='POINT', location=(-3, -2, 3))\n"
        "bpy.context.object.data.energy = 2000\n"
        "bpy.context.object.data.color = (0.7, 0.3, 1)",
    ),
    (
        "spin the monkey",
        "monkey = bpy.data.objects['Suzanne']\n"
        "monkey.keyframe_insert('rotation_euler', frame=1)\n"
        "monkey.rotation_euler.z = 2 * math.pi\n"
        "monkey.keyframe_insert('rotation_euler', frame=120)\n"
        "bpy.context.scene.frame_end = 120\n"
        "bpy.ops.screen.animation_play()",
    ),
]


def blender(url: str, code: str | None = None, timeout: int = 300) -> str:
    """GET the renderer name, or POST a bpy snippet and return its output."""
    data = code.encode() if code else None
    return urllib.request.urlopen(url, data=data, timeout=timeout).read().decode().strip()


@app.local_entrypoint()
def main(gpu: str = "L4", region: str = "", minutes: int = 10):
    started = time.monotonic()
    sandbox = modal.Sandbox.create(
        "bash", "-c", DESKTOP,
        app=app, image=image, gpu=gpu, cpu=4, region=region or None,
        encrypted_ports=[6080, 8000],  # noVNC, and the snippet server inside Blender
        timeout=minutes * 60 + 600,
    )  # fmt: skip
    try:
        tunnels = sandbox.tunnels()
        api = tunnels[8000].url
        print(f"\nOpen in a browser: {tunnels[6080].url}/vnc.html?autoconnect=1&resize=scale")
        print(f"POST bpy code to:  {api}\n")

        # Blender is ready once its snippet server answers.
        deadline = time.monotonic() + 300
        while True:
            try:
                renderer = blender(api)
                print(
                    f"Blender ready after {time.monotonic() - started:.0f} s, viewport renderer: {renderer}"
                )
                break
            except (urllib.error.URLError, TimeoutError):
                if time.monotonic() > deadline:
                    raise RuntimeError(sandbox.exec("tail", "-30", "/tmp/blender.log").stdout.read())
                time.sleep(2)

        for what, code in EDITS:
            time.sleep(4)
            out = blender(api, code)
            print(f"{what}: {out or 'ok'}")

        print(f"\nYour turn: click around in the browser. Shutting down in {minutes} minutes.")
        time.sleep(minutes * 60)
    finally:
        sandbox.terminate()
