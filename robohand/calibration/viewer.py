"""Saved RGB-D observations overlaid with a metric virtual ChArUco board."""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys

import cv2
import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg
import pyqtgraph.opengl as gl

import pyrealsense2 as rs

from ..cameras.hub import CAMERA_KEYS, CAMERA_NAMES
from ..paths import ACTIVE_CALIBRATION, configure_logging
from ..replay.reconstruction import camera_rays
from ..ui.theme import FONT
from .board import (depth_plane_validation, detect_corners, draw_overlay, load_calibration,
                    reprojection_errors, sdk_intrinsics)

CAMERA_COLORS = [(1., .4, .3, .75), (.3, 1., .6, .75), (.4, .65, 1., .75)]


class CalibrationViewer(QtWidgets.QMainWindow):
    def __init__(self, folder, calibration_path=ACTIVE_CALIBRATION):
        super().__init__()
        self.folder = Path(folder)
        self.calibration = load_calibration(calibration_path)
        if not self.calibration:
            raise ValueError("보정 파일을 읽을 수 없습니다.")
        self.sequence = (self.folder/"capture.json").exists()
        manifest = self.folder/('capture.json' if self.sequence else 'metadata.json')
        self.metadata = json.loads(manifest.read_text(encoding="utf-8"))
        self.count = len(self.metadata["samples"]) if self.sequence else 1
        self.rays, self.images, self.clouds, self.cloud_data = {}, {}, {}, {}
        self.model_items, self.enabled, self.metrics = [], {}, {}
        self.setWindowTitle("Real / Sim | 3 RGB-D Calibration")
        self.resize(1480, 1000)
        self.setStyleSheet("QWidget {background:#101923;color:#edf5fc;font-size:12px;}"
                           "QPushButton {background:#263746;padding:7px;} QSlider {min-height:22px;}")
        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        layout = QtWidgets.QVBoxLayout(root)
        toolbar = QtWidgets.QHBoxLayout()
        square_mm = self.calibration["board"]["square_m"]*1000
        title = QtWidgets.QLabel(f"REAL / SIM · {square_mm:.3f} mm ChArUco · 보드 좌표계 (m)")
        title.setStyleSheet("font-size:17px;font-weight:600;padding:5px;")
        toolbar.addWidget(title)
        toolbar.addStretch()
        self.rgb = QtWidgets.QCheckBox("실제 RGB 색상")
        self.rgb.setChecked(True)
        self.rgb.toggled.connect(self.update_cloud_colors)
        toolbar.addWidget(self.rgb)
        model = QtWidgets.QCheckBox("가상 보드 / 50 mm 큐브")
        model.setChecked(True)
        model.toggled.connect(lambda shown: [item.setVisible(shown) for item in self.model_items])
        toolbar.addWidget(model)
        reset = QtWidgets.QPushButton("시점 초기화")
        reset.clicked.connect(self.reset_view)
        toolbar.addWidget(reset)
        layout.addLayout(toolbar)
        images = QtWidgets.QHBoxLayout()
        for index, key in enumerate(CAMERA_KEYS):
            info = self.metadata["devices"][key]
            if info.get("serial") != self.calibration["cameras"][key]["serial"]:
                raise ValueError(f"{key}: 카메라 S/N이 보정 데이터와 다릅니다.")
            column = QtWidgets.QVBoxLayout()
            check = QtWidgets.QCheckBox(f"{CAMERA_NAMES[key]} · {info['serial']}")
            check.setChecked(True)
            check.toggled.connect(lambda shown, camera=key: self.clouds[camera].setVisible(shown))
            self.enabled[key] = check
            column.addWidget(check)
            image = QtWidgets.QLabel()
            image.setMinimumSize(240, 150)
            image.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.images[key] = image
            column.addWidget(image)
            self.metrics[key] = QtWidgets.QLabel()
            self.metrics[key].setStyleSheet("color:#b9cedf;padding:3px;")
            column.addWidget(self.metrics[key])
            images.addLayout(column)
        layout.addLayout(images)
        self.view = gl.GLViewWidget()
        self.view.setBackgroundColor((8, 14, 21))
        self.view.setMinimumHeight(350)
        layout.addWidget(self.view, 1)
        self.build_model()
        for key, color in zip(CAMERA_KEYS, CAMERA_COLORS):
            self.clouds[key] = gl.GLScatterPlotItem(pos=np.empty((0, 3)), color=color, size=2,
                                                   pxMode=True, glOptions="opaque")
            self.view.addItem(self.clouds[key])
            self.add_camera(key, color)
        self.reset_view()
        controls = QtWidgets.QHBoxLayout()
        self.frame_label = QtWidgets.QLabel()
        controls.addWidget(self.frame_label)
        self.slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.slider.setRange(0, self.count-1)
        self.slider.valueChanged.connect(self.load_frame)
        controls.addWidget(self.slider, 1)
        layout.addLayout(controls)
        layout.addWidget(QtWidgets.QLabel("드래그 회전 · 휠 확대 · 가운데 버튼 이동 · 실제 depth와 가상 평면 Z=0 중첩"))
        self.statusBar().showMessage(str(self.folder))
        self.load_frame(0)

    def reset_view(self):
        self.view.opts['center'] = pg.Vector(.08, -.06, .12)
        self.view.setCameraPosition(distance=1.15, elevation=45, azimuth=-55)

    def add_lines(self, points, color, model=True, width=1):
        item = gl.GLLinePlotItem(pos=np.asarray(points, dtype=float), color=color, width=width,
                                mode="lines", antialias=True)
        self.view.addItem(item)
        if model:
            self.model_items.append(item)

    def build_model(self):
        vertices, faces, colors = [], [], []
        board = self.calibration["board"]
        columns, rows = board["squares"]
        square = board["square_m"]
        width, height = columns*square, rows*square
        for row in range(rows):
            for column in range(columns):
                x, y = column*square, -row*square
                start = len(vertices)
                vertices.extend([[x,y,0], [x+square,y,0], [x+square,y-square,0], [x,y-square,0]])
                faces.extend([[start,start+1,start+2], [start,start+2,start+3]])
                shade = .85 if (row+column) % 2 else .12
                colors.extend([(shade,shade,shade,.65)]*2)
        mesh = gl.GLMeshItem(vertexes=np.asarray(vertices), faces=np.asarray(faces),
                             faceColors=np.asarray(colors), smooth=False, drawEdges=False)
        mesh.setGLOptions("translucent")
        self.view.addItem(mesh)
        self.model_items.append(mesh)
        grid = []
        for column in range(columns+1):
            grid.extend([[column*square,0,0], [column*square,-height,0]])
        for row in range(rows+1):
            grid.extend([[0,-row*square,0], [width,-row*square,0]])
        self.add_lines(grid, (.25,.9,1.,1.))
        for point, color in [([.12,0,0],(1,.25,.2,1)), ([0,.12,0],(.2,1,.4,1)),
                             ([0,0,.12],(.3,.5,1,1))]:
            self.add_lines([[0,0,0],point], color, width=3)
        base = np.array([[.05,-.05,0],[.1,-.05,0],[.1,-.1,0],[.05,-.1,0]])
        top = base + [0,0,.05]
        cube = []
        for index in range(4):
            cube.extend([base[index],base[(index+1)%4],top[index],top[(index+1)%4],base[index],top[index]])
        self.add_lines(cube, (1.,.35,1.,1.), width=2)

    def add_camera(self, key, color):
        info = self.metadata["devices"][key]
        intr = sdk_intrinsics(info["color_intrinsics"])
        transform = np.asarray(self.calibration["cameras"][key]["T_board_from_camera"])
        corners = [[0,0],[intr.width-1,0],[intr.width-1,intr.height-1],[0,intr.height-1]]
        points = np.asarray([rs.rs2_deproject_pixel_to_point(intr, pixel, .08) for pixel in corners])
        points = points @ transform[:3,:3].T + transform[:3,3]
        lines = []
        for index in range(4):
            lines.extend([transform[:3,3],points[index],points[index],points[(index+1)%4]])
        self.add_lines(lines, color, model=False, width=2)

    def load_frame(self, index):
        self.frame_label.setText(f"프레임 {index+1} / {self.count} · 저장된 정지 보드 관측")
        source = self.folder/f"frame_{index:03d}" if self.sequence else self.folder
        for key in CAMERA_KEYS:
            info = self.metadata["devices"][key]
            camera = self.calibration["cameras"][key]
            image = cv2.imread(str(source/f"{key}_color.png"))
            depth_suffix = "depth.npy" if self.sequence else "depth_raw.npy"
            depth = np.load(source/f"{key}_{depth_suffix}")
            observations = detect_corners(image, self.calibration["board"])
            overlay = draw_overlay(image, camera, info["color_intrinsics"], observations)
            rgb = cv2.cvtColor(overlay, cv2.COLOR_BGR2RGB)
            qimage = QtGui.QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QtGui.QImage.Format.Format_RGB888).copy()
            self.images[key].setPixmap(QtGui.QPixmap.fromImage(qimage).scaled(
                440, 250, QtCore.Qt.AspectRatioMode.KeepAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation))
            if key not in self.rays:
                self.rays[key] = camera_rays(info["color_intrinsics"])[::3,::3]
            depths = depth[::3,::3]
            points = self.rays[key] * (depths*info["depth_scale_m"])[...,None]
            transform = np.asarray(camera["T_board_from_camera"])
            points = points.reshape(-1,3) @ transform[:3,:3].T + transform[:3,3]
            valid = (depths.ravel()>0) & (depths.ravel()<65535)
            valid &= (points[:,0]>-.45) & (points[:,0]<.65) & (points[:,1]>-.6) & (points[:,1]<.45)
            valid &= (points[:,2]>-.06) & (points[:,2]<.7)
            colors = np.ones((np.count_nonzero(valid), 4), dtype=np.float32)
            colors[:, :3] = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)[::3, ::3].reshape(-1, 3)[valid] / 255.
            self.cloud_data[key] = (points[valid], colors)
            error = reprojection_errors([observations], camera, info["color_intrinsics"])
            plane = depth_plane_validation(image, depth, camera, info)
            pixels = f"{error['rms']:.2f} px" if error.get("count") else "코너 미검출"
            millimeters = f"{plane['median']:.1f} mm" if plane.get("count") else "깊이 없음"
            self.metrics[key].setText(f"재투영 RMS {pixels}  ·  평면 |오차| 중앙값 {millimeters}")
        self.update_cloud_colors()

    def update_cloud_colors(self):
        for key, color in zip(CAMERA_KEYS,CAMERA_COLORS):
            if key in self.cloud_data:
                points, colors = self.cloud_data[key]
                self.clouds[key].setData(pos=points, color=colors if self.rgb.isChecked() else color)


def main():
    parser = argparse.ArgumentParser(description="Real / Sim 3D view of a snapshot or calibration capture")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--calibration", type=Path, default=ACTIVE_CALIBRATION)
    arguments = parser.parse_args()
    configure_logging("calibration_viewer")
    app = QtWidgets.QApplication(sys.argv[:1])
    app.setFont(QtGui.QFont(FONT, 9))
    try:
        window = CalibrationViewer(arguments.folder, arguments.calibration)
        window.show()
        return app.exec()
    except Exception as error:
        logging.exception("Calibration viewer failed")
        QtWidgets.QMessageBox.critical(None, "Real / Sim", str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
