"""Suturing workspace in MuJoCo: table, ChArUco board, suture pad, needle holder and a robot hand
(Shadow Hand by default, Tesollo DG-5F-M on request).

World frame = calibration board frame (X along columns, Y opposite rows, Z up, metres; the board
covers X 0..0.217, Y -0.155..0). The pad is a rigid box for now. The hand hangs on a free joint
that is welded to the mocap body ``hand_target``: move the target to drive the wrist, set
``ctrl`` to drive the fingers.

    python -m robohand.sim.scene --render workspace.png --xml workspace.xml [--hand dg5f]
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import mujoco
import numpy as np

from robohand.sim import dg5f, shadow
from robohand.sim.needle_holder import NeedleHolderSpec, Ratchet, mjcf as needle_holder_mjcf

BOARD = (0.217, 0.155)    # 7 x 5 squares of 31 mm (calibration of 2026-11-26)
# Each loader returns (spec, root body); both hands put the fingers along +z and the palm on +x
# of their root frame once the root's own rotation is cleared.
HANDS = {"shadow": shadow.load, "dg5f": dg5f.load}
HOME = {"shadow": (-0.42, 0.075, 0.13), "dg5f": (-0.16, 0.075, 0.13)}   # root position, palm down


@dataclass(frozen=True)
class PadSpec:
    """Silicone suture pad in its clear tray (dataset tool_images/IMG_4627, 4628), millimetres."""
    length: float = 154.0
    width: float = 81.0
    thickness: float = 10.0        # not measured
    tray_length: float = 157.0
    tray_width: float = 85.0
    tray_wall: float = 1.5         # not measured
    center: tuple = (108.5, -77.5)  # board centre until the pad is registered from the cameras


def _base_xml(pad: PadSpec, hand_root: str) -> str:
    mm = 1e-3
    cx, cy = pad.center[0] * mm, pad.center[1] * mm
    tl, tw, tt = pad.tray_length / 2 * mm, pad.tray_width / 2 * mm, pad.tray_wall / 2 * mm
    h = pad.thickness / 2 * mm
    return f"""
<mujoco model="suturing_workspace">
  <compiler angle="radian"/>
  <option timestep="0.002" integrator="implicitfast" cone="elliptic" impratio="10"/>
  <visual><global offwidth="1600" offheight="1200"/><quality shadowsize="4096"/></visual>
  <asset>
    <texture name="board" type="2d" builtin="checker" rgb1="0.95 0.95 0.95" rgb2="0.15 0.15 0.15"
             width="512" height="512"/>
    <material name="board" texture="board" texrepeat="3.5 2.5"/>
    <material name="table" rgba="0.92 0.92 0.90 1"/>
    <material name="silicone" rgba="0.94 0.66 0.58 1"/>
    <material name="tray" rgba="0.85 0.88 0.92 0.35"/>
  </asset>
  <worldbody>
    <light pos="0.1 -0.3 0.8" dir="0 0.3 -1" diffuse="0.8 0.8 0.8"/>
    <light pos="-0.4 0.2 0.6" dir="0.5 -0.2 -1" diffuse="0.3 0.3 0.3" castshadow="false"/>
    <camera name="overview" mode="targetbody" target="pad" pos="0.45 -0.42 0.42"/>
    <camera name="instrument" mode="targetbody" target="nh_needle_holder" pos="0.16 -0.06 0.16"/>
    <geom name="table" type="box" pos="0.1 -0.08 -0.01" size="0.6 0.4 0.01" material="table"/>
    <geom name="board" type="box" pos="{BOARD[0] / 2:.4f} {-BOARD[1] / 2:.4f} 0.0002"
          size="{BOARD[0] / 2:.4f} {BOARD[1] / 2:.4f} 0.0002" material="board" contype="0" conaffinity="0"/>
    <body name="pad" pos="{cx:.4f} {cy:.4f} 0">
      <geom name="tray_floor" type="box" pos="0 0 {tt:.4f}" size="{tl:.4f} {tw:.4f} {tt:.4f}" material="tray"/>
      <geom name="pad" type="box" pos="0 0 {2 * tt + h:.4f}" size="{pad.length / 2 * mm:.4f} {pad.width / 2 * mm:.4f} {h:.4f}"
            material="silicone" friction="1.2 0.01 0.0002"/>
      <site name="pad_top" pos="0 0 {2 * tt + 2 * h:.4f}" size="0.001" group="4"/>
    </body>
    <body name="hand_target" mocap="true"/>
  </worldbody>
  <equality>
    <!-- resolved after the hand is attached; relpose comes from qpos0 (both at the origin) -->
    <weld name="hand_follow" body1="hand_{hand_root}" body2="hand_target" solref="0.02 1"/>
  </equality>
</mujoco>"""


def build(hand: str = "shadow", side: str = "right", pad: PadSpec = PadSpec(),
          needle_holder: NeedleHolderSpec = NeedleHolderSpec()) -> mujoco.MjSpec:
    """Scene spec; names from the attached models are prefixed ``nh_`` and ``hand_``
    (the hand's free joint is ``hand_free``)."""
    hand_spec, root_name = HANDS[hand](side)
    spec = mujoco.MjSpec.from_string(_base_xml(pad, root_name))
    instrument = mujoco.MjSpec.from_string(needle_holder_mjcf(needle_holder))
    spec.worldbody.add_frame().attach_body(instrument.body("needle_holder"), "nh_", "")
    root = hand_spec.body(root_name)
    root.pos, root.quat = [0, 0, 0], [1, 0, 0, 0]     # pose comes from the free joint / target
    root.add_freejoint(name="free")
    spec.worldbody.add_frame().attach_body(root, "hand_", "")
    return spec


def palm_down(fingers_along=(1.0, 0.0, 0.0)) -> np.ndarray:
    """Quaternion that turns the hand palm-down with the fingers along ``fingers_along``."""
    z = np.asarray(fingers_along, float)
    z /= np.linalg.norm(z)
    x = np.array([0.0, 0.0, -1.0])        # palm normal (+x of the hand) points at the table
    y = np.cross(z, x)
    quat = np.zeros(4)
    mujoco.mju_mat2Quat(quat, np.column_stack([x, y, z]).ravel())
    return quat


def place(model: mujoco.MjModel, data: mujoco.MjData, hand_pos, hand_quat,
          instrument_pos=(0.04, 0.075, 0.004), instrument_yaw=0.0) -> None:
    """Initial state: needle holder lying on the table, hand at its target, fingers open."""
    nh = model.joint("nh_needle_holder")
    half = instrument_yaw / 2
    data.qpos[nh.qposadr[0]:nh.qposadr[0] + 7] = [*instrument_pos, np.cos(half), 0, 0, np.sin(half)]
    hand = model.joint("hand_free")
    data.qpos[hand.qposadr[0]:hand.qposadr[0] + 7] = [*hand_pos, *hand_quat]
    target = model.body("hand_target").mocapid[0]
    data.mocap_pos[target], data.mocap_quat[target] = hand_pos, hand_quat
    data.ctrl[:] = 0.0
    mujoco.mj_forward(model, data)


def render(model: mujoco.MjModel, data: mujoco.MjData, path: Path, camera: str = "overview",
           size=(1280, 960)) -> Path:
    from PIL import Image
    with mujoco.Renderer(model, size[1], size[0]) as renderer:
        renderer.update_scene(data, camera)
        Image.fromarray(renderer.render()).save(path)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hand", default="shadow", choices=tuple(HANDS))
    parser.add_argument("--side", default="right", choices=("right", "left"))
    parser.add_argument("--seconds", type=float, default=1.0, help="simulate before rendering")
    parser.add_argument("--render", type=Path, help="write a PNG")
    parser.add_argument("--camera", default="overview", choices=("overview", "instrument"))
    parser.add_argument("--xml", type=Path, help="write the compiled scene as MJCF")
    args = parser.parse_args()

    spec = build(args.hand, args.side)
    model = spec.compile()
    data = mujoco.MjData(model)
    place(model, data, hand_pos=HOME[args.hand], hand_quat=palm_down())
    ratchet = Ratchet(model, "nh_")
    ratchet.try_lock(model, data)
    for _ in range(int(args.seconds / model.opt.timestep)):
        mujoco.mj_step(model, data)
    print(f"bodies {model.nbody}, joints {model.njnt}, actuators {model.nu}, "
          f"needle holder at {data.body('nh_needle_holder').xpos.round(4)}, "
          f"boxlock {np.degrees(ratchet.angle(data)):.2f} deg (ratchet {'on' if ratchet.locked(data) else 'off'}), "
          f"contacts {data.ncon}")
    if args.xml:
        args.xml.write_text(spec.to_xml())
    if args.render:
        print("rendered", render(model, data, args.render, args.camera))


if __name__ == "__main__":
    main()
