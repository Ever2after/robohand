"""Tesollo DG-5F-M hand, built from Tesollo's own model files (BSD-3-Clause).

The files are downloaded once from github.com/tesollodelto/tesollo_model at a pinned commit
into DATA_ROOT/sim_assets/. The upstream MJCF is kinematic only (no actuators, the palm geoms
sit directly in the world body), so ``load`` wraps the hand in a root body and adds joint
damping, rotor armature and one position actuator per joint.

Numbers from the DG-5F-M hardware manual v2.0.0: 20 joints (4 per finger), BLDC per joint,
rated 0.4 N m / peak 2 N m per joint, 97 N maximum fingertip force, 250 Hz control,
fingertips of 40 Shore A silicone. Finger links are 22.4 x 19.0 mm in cross section
(measured from the meshes); the fingertip caps are 21.5 x 10.9 mm.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

import mujoco

from robohand.sim.assets import absolute_mesh_paths, fetch

COMMIT = "2ce9aca00945af09bb1ad038f3f509824167119e"
RAW = f"https://raw.githubusercontent.com/tesollodelto/tesollo_model/{COMMIT}"
LINKS = ["link_mount", "link_base", "link_palm"] + [
    f"link_{finger}_{part}" for finger in range(1, 6) for part in ("1", "2", "3", "4", "tip")]

# Joint servo: stiff enough to hold a pose against gravity and light contact, limited to the
# peak torque. Armature stands in for the reflected rotor inertia of the geared BLDC drives.
KP = 6.0            # N m / rad
DAMPING = 0.08      # N m s / rad
ARMATURE = 0.002    # kg m^2
FINGERTIP_FRICTION = "1.0 0.01 0.0002"


def ensure(side: str = "right"):
    files = [f"dg5f/dg5f_{side}.xml", "LICENSE"] + [f"dg5f/meshes/dg5f_{side}/{link}.STL" for link in LINKS]
    return fetch(f"tesollo_model-{COMMIT[:8]}", RAW, files) / "dg5f" / f"dg5f_{side}.xml"


def load(side: str = "right") -> tuple[mujoco.MjSpec, str]:
    """The hand as an MjSpec and its root body ``root`` (the mount flange, fingers along +z,
    palm facing +x); actuators are named after their joints."""
    path = ensure(side)
    model = ET.parse(path).getroot()
    absolute_mesh_paths(model, path.parent / "meshes")
    world = model.find("worldbody")
    root = ET.Element("body", name="root")
    for child in list(world):
        world.remove(child)
        root.append(child)
    world.append(root)
    # MuJoCo already skips parent-child contacts; the upstream meshes also overlap one link
    # further (palm and the second link of thumb and ring finger at the zero pose), so skip
    # grandparent-grandchild pairs too.
    contact = ET.SubElement(model, "contact")
    parents = {child: parent for parent in model.iter("body") for child in parent.findall("body")}
    for body, parent in parents.items():
        if parent in parents:
            ET.SubElement(contact, "exclude", body1=parents[parent].get("name"), body2=body.get("name"))
    actuators = ET.SubElement(model, "actuator")
    for joint in model.iter("joint"):
        joint.set("damping", str(DAMPING))
        joint.set("armature", str(ARMATURE))
        ET.SubElement(actuators, "position", name=joint.get("name"), joint=joint.get("name"),
                      kp=str(KP), ctrlrange=joint.get("range"), forcerange="-2 2")
    for geom in model.iter("geom"):
        if geom.get("mesh", "").endswith("_tip"):
            geom.set("friction", FINGERTIP_FRICTION)
            geom.set("condim", "4")
    return mujoco.MjSpec.from_string(ET.tostring(model, encoding="unicode")), "root"
