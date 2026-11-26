"""Shadow Dexterous Hand E3M5 from MuJoCo Menagerie (Apache-2.0), the simulation reference hand.

Menagerie's model already carries tuned position actuators: 20 actuators for 24 joints, the
middle and distal joints of the four fingers sharing one tendon each (as on the real hand),
plus two wrist joints. The root body is the forearm, ``rh_forearm`` / ``lh_forearm``.
Its fingers are 14-18 mm across and pass the needle-holder rings (27 x 21 mm) to the middle
phalanx; the thumb tip is up to 22 mm and passes too.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

import mujoco

from robohand.sim.assets import absolute_mesh_paths, fetch

COMMIT = "eea4c7431b63da8278b0ff31638e0b0e941cec0e"
RAW = f"https://raw.githubusercontent.com/google-deepmind/mujoco_menagerie/{COMMIT}"
MESHES = ["f_distal_pst", "f_knuckle", "f_middle", "f_proximal", "forearm_0", "forearm_1",
          "forearm_collision", "lf_metacarpal", "mounting_plate", "palm", "th_distal_pst",
          "th_middle", "th_proximal", "wrist"]


def ensure(side: str = "right"):
    files = [f"shadow_hand/{side}_hand.xml", "shadow_hand/LICENSE"] + [f"shadow_hand/assets/{m}.obj" for m in MESHES]
    return fetch(f"mujoco_menagerie-{COMMIT[:8]}", RAW, files) / "shadow_hand" / f"{side}_hand.xml"


def load(side: str = "right") -> tuple[mujoco.MjSpec, str]:
    """The hand as an MjSpec and the name of its root body."""
    path = ensure(side)
    model = ET.parse(path).getroot()
    absolute_mesh_paths(model, path.parent / "assets")
    return mujoco.MjSpec.from_string(ET.tostring(model, encoding="unicode")), f"{side[0]}h_forearm"
