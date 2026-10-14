# Remote Cycles

![Blender 5.2](https://img.shields.io/badge/Blender-5.2-orange?logo=blender&logoColor=white)
![License: GPL-3.0](https://img.shields.io/badge/license-GPL--3.0-blue)

Render Cycles on other computers on your local network, from inside Blender.

- **Viewport**: rendered shading streams from another machine's GPU while you work.
- **F12**: renders on the fastest machine, or splits the image across machines of similar speed.
- **Ctrl+F12**: every machine renders frames in parallel, one window per machine.
- Machines find each other automatically, no IP addresses needed.

## Installation

Requires **Blender 5.2.2 LTS** on every machine.

1. Download the latest `remote_cycles-(whatever the newest version is).zip` from
   [Releases](https://github.com/maksym-chubatyuk/blender_remote_cycles/releases).
2. In Blender: **Edit → Preferences → Add-ons → Install from Disk**
   and pick the zip.

Do this on every machine.

## Usage

1. **On the computer with your project**, set **Render Engine** to **Remote Cycles**.
2. **On each other computer**, open Blender, press **N** in the 3D viewport, open the
   **Cluster** tab and click **Join**.
   That Blender loads your project in place of its open file and renders for you until you
   click **Leave**. It rejoins on its own next time.
3. Render as usual: Rendered viewport, F12, Ctrl+F12.

## Settings

In **Render Properties → Render Cluster** on the project computer:

- **Machine and device checkboxes**: which computers, and which of their GPUs/CPU, take part.
- **Render on This Machine**: also use the project computer's own devices.
- **Still Images**: how F12 uses the machines.
  - **Auto** splits an image only across machines of similar speed, and only for slow renders.
  - **Fastest Machine** always renders the whole image on the fastest machine.
  - **All Machines** always splits it.

## License

[GPL-3.0-or-later](LICENSE)
