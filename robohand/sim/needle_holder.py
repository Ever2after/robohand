"""Parametric, articulated model of the needle holder used in the recordings.

Instrument frame (the body frame of half A): origin at the box-lock pivot, +x towards the jaw
tip, +y across the instrument in the plane of the rings, +z along the hinge axis. The spec is in
millimetres; the MJCF is in metres.

The two halves cross at the pivot like scissors: half A has its jaw on +y and its ring on -y,
half B is the mirror image and hinges on the pivot. Joint ``boxlock`` is 0 with the jaws closed
and grows as they open. The ratchet is a joint-equality constraint (inactive by default) that
``Ratchet`` switches on to hold the jaws at an angle.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import mujoco

STAINLESS_DENSITY = 7900.0   # kg/m^3


@dataclass(frozen=True)
class NeedleHolderSpec:
    """Dimensions in millimetres, read from tool_images/IMG_4630 (tape measure in frame).

    The photo is not perfectly square to the table, so lengths carry about +-3 % error.
    Fields marked "not measured" are typical values for an 18 cm Mayo-Hegar and must be
    replaced with calliper readings.
    """
    length: float = 178.0               # jaw tip to ring end (~18 cm catalogue size)
    tip_to_pivot: float = 33.0          # rivet not visible in the photo: verify
    jaw_length: float = 21.0            # serrated part
    jaw_width: float = 5.0              # both jaws together, in plane (3.5 at tip, 6.5 at base)
    jaw_thickness: float = 4.0          # not measured
    box_length: float = 25.0            # between the two shoulders of the box lock
    box_width: float = 7.0
    box_thickness: float = 5.0          # not measured
    shank_diameter: float = 3.5         # ~4 mm wide in plane; thickness not measured
    ring_center_from_tip: float = 162.0
    ring_offset: float = 25.7           # each ring centre from the instrument axis, jaws closed
    ring_inner_long: float = 27.0       # finger opening along the instrument axis
    ring_inner_short: float = 21.0      # finger opening across it
    ring_wire: float = 2.5              # ~2.0 in plane; thickness not measured
    ratchet_from_tip: float = 147.0
    ratchet_length: float = 14.0
    max_opening_deg: float = 35.0       # not measured
    ratchet_engage_deg: float = 3.0     # teeth overlap only when the jaws are nearly closed
    mass_g: float | None = None         # weigh it; None uses stainless-steel density
    ring_segments: int = 16


def _f(*values):
    return " ".join(f"{v:.5f}" for v in values)


def _half(spec: NeedleHolderSpec, side: int) -> str:
    """Geoms and sites of one half. side=+1 for half A (jaw on +y), -1 for half B."""
    mm = 1e-3
    s = side
    jaw_x = (spec.tip_to_pivot - spec.jaw_length / 2) * mm
    jaw_y = s * spec.jaw_width / 4 * mm
    box_end = (spec.tip_to_pivot - spec.jaw_length - spec.box_length) * mm
    ring_cx = (spec.tip_to_pivot - spec.ring_center_from_tip) * mm
    ring_cy = -s * spec.ring_offset * mm
    a = (spec.ring_inner_long + spec.ring_wire) / 2 * mm       # ring centreline semi-axes
    b = (spec.ring_inner_short + spec.ring_wire) / 2 * mm
    wire = spec.ring_wire / 2 * mm
    # The shank leaves the box lock just off the axis and joins the distal, inner side of the ring.
    phi = math.radians(40) * s
    joint_x, joint_y = ring_cx + a * math.cos(phi), ring_cy + b * math.sin(phi)
    shank0 = (box_end, -s * spec.box_width / 4 * mm)
    name = "a" if side > 0 else "b"
    geoms = [
        f'<geom name="jaw_{name}" class="nh_jaw" type="box" pos="{_f(jaw_x, jaw_y, 0)}" '
        f'size="{_f(spec.jaw_length / 2 * mm, spec.jaw_width / 4 * mm, spec.jaw_thickness / 2 * mm)}"/>',
        f'<geom name="shank_{name}" class="nh" type="capsule" size="{spec.shank_diameter / 2 * mm:.5f}" '
        f'fromto="{_f(shank0[0], shank0[1], 0, joint_x, joint_y, 0)}"/>',
    ]
    if side > 0:   # the box lock belongs to half A; half B passes through it
        geoms.append(f'<geom name="box_lock" class="nh" type="box" '
                     f'pos="{_f((spec.tip_to_pivot - spec.jaw_length - spec.box_length / 2) * mm, 0, 0)}" '
                     f'size="{_f(spec.box_length / 2 * mm, spec.box_width / 2 * mm, spec.box_thickness / 2 * mm)}"/>')
    n = spec.ring_segments
    for i in range(n):
        t0, t1 = 2 * math.pi * i / n, 2 * math.pi * (i + 1) / n
        p0 = (ring_cx + a * math.cos(t0), ring_cy + b * math.sin(t0))
        p1 = (ring_cx + a * math.cos(t1), ring_cy + b * math.sin(t1))
        geoms.append(f'<geom name="ring_{name}{i}" class="nh" type="capsule" size="{wire:.5f}" '
                     f'fromto="{_f(p0[0], p0[1], 0, p1[0], p1[1], 0)}"/>')
    # Ratchet teeth: a bar from the shank towards the axis, the two halves side by side in x.
    rx = (spec.tip_to_pivot - spec.ratchet_from_tip) * mm + s * 2e-3
    t = (rx - shank0[0]) / (joint_x - shank0[0])
    shank_y = shank0[1] + t * (joint_y - shank0[1])
    inner = s * 0.5e-3
    geoms.append(f'<geom name="ratchet_{name}" class="nh" type="box" '
                 f'pos="{_f(rx, (shank_y + inner) / 2, 0)}" '
                 f'size="{_f(1.5e-3, abs(shank_y - inner) / 2, 1.0e-3)}"/>')
    sites = [
        f'<site name="tip_{name}" pos="{_f(spec.tip_to_pivot * mm, jaw_y, 0)}"/>',
        f'<site name="ring_{name}" pos="{_f(ring_cx, ring_cy, 0)}"/>',
    ]
    return "\n".join(geoms + sites)


def mjcf(spec: NeedleHolderSpec = NeedleHolderSpec(), density: float | None = None) -> str:
    """Stand-alone MJCF of the needle holder on a free joint (root body ``needle_holder``)."""
    density = density or _density(spec)
    mm = 1e-3
    return f"""
<mujoco model="needle_holder">
  <compiler angle="radian"/>
  <default>
    <default class="nh">
      <geom density="{density:.1f}" friction="0.5 0.005 0.0001" condim="4" rgba="0.76 0.77 0.80 1"/>
      <site size="0.0008" group="4" rgba="1 0.2 0.2 1"/>
      <default class="nh_jaw">
        <geom friction="1.0 0.01 0.0001" rgba="0.55 0.56 0.60 1"/>
      </default>
    </default>
  </default>
  <worldbody>
    <body name="needle_holder" childclass="nh">
      <freejoint name="needle_holder"/>
      {_half(spec, +1)}
      <site name="pivot" pos="0 0 0"/>
      <site name="jaw_mid" pos="{_f((spec.tip_to_pivot - spec.jaw_length / 2) * mm, 0, 0)}"/>
      <body name="needle_holder_b">
        <joint name="boxlock" type="hinge" axis="0 0 -1" range="0 {math.radians(spec.max_opening_deg):.5f}"
               damping="0.002" frictionloss="0.003" armature="1e-5" solreflimit="0.004 1"/>
        {_half(spec, -1)}
      </body>
    </body>
  </worldbody>
  <equality>
    <joint name="ratchet" joint1="boxlock" polycoef="0 0 0 0 0" active="false" solref="0.005 1"/>
  </equality>
</mujoco>"""


def _density(spec: NeedleHolderSpec) -> float:
    if spec.mass_g is None:
        return STAINLESS_DENSITY
    probe = mujoco.MjModel.from_xml_string(mjcf(spec, density=1000.0))
    volume = probe.body_mass[1:].sum() / 1000.0
    return spec.mass_g * 1e-3 / volume


class Ratchet:
    """Holds the jaws at an angle the way the ratchet teeth do. ``prefix`` is the attach prefix."""

    def __init__(self, model: mujoco.MjModel, prefix: str = "", spec: NeedleHolderSpec = NeedleHolderSpec()):
        self.eq = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_EQUALITY, f"{prefix}ratchet")
        joint = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, f"{prefix}boxlock")
        if self.eq < 0 or joint < 0:
            raise KeyError(f"no needle holder with prefix {prefix!r} in this model")
        self.qadr = model.jnt_qposadr[joint]
        self.engage = math.radians(spec.ratchet_engage_deg)

    def angle(self, data: mujoco.MjData) -> float:
        return float(data.qpos[self.qadr])

    def locked(self, data: mujoco.MjData) -> bool:
        return bool(data.eq_active[self.eq])

    def try_lock(self, model: mujoco.MjModel, data: mujoco.MjData) -> bool:
        """Engage at the current angle if the jaws are closed far enough for the teeth to meet."""
        if self.angle(data) > self.engage:
            return False
        model.eq_data[self.eq, 0] = self.angle(data)
        data.eq_active[self.eq] = 1
        return True

    def release(self, data: mujoco.MjData) -> None:
        data.eq_active[self.eq] = 0
