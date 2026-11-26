"""Offline multi-view reconstruction from recorded sessions (hands now; instruments next).

All results are in the calibration board frame (metres) on the session's host clock, the same
frame that ``robohand.sim`` uses as its world. Needs numpy, scipy, opencv, av, onnxruntime and
rtmlib (requirements-recon.txt); pyrealsense2 is not needed.
"""
