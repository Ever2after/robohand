"""Real2sim in MuJoCo: surgical instruments, the suture pad and the Tesollo DG-5F-M hand.

The simulation world frame is the calibration board frame (X along the board columns,
Y opposite the rows, Z up, metres), so poses reconstructed from the cameras drop in unchanged.
Needs only mujoco, numpy and Pillow (requirements-sim.txt); it runs on macOS as well as Windows.
"""
