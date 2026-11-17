"""Interactive GPU point-cloud replay with a frame timeline and multi-camera overlay."""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
import sys
import threading
import time

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph.opengl as gl

from ..devices.manus import FINGER_COLORS, load_manus, skeleton_layout
from ..paths import APP_ICON, RECORDINGS, configure_logging
from ..ui.theme import FONT
from .reconstruction import CALIBRATION_ORIGINS, CloudFrame, MultiSequence, latest_recording, write_ply

CAMERA_COLORS = [(1., .4, .3), (.3, 1., .6), (.4, .65, 1.)]
BOARD_FRUSTUM_M = .1
GLOVE_OFFSET_M = .17      # wrists drawn this far left/right of the glove view centre
GLOVE_MAX_GAP_MS = 100    # hide a hand when no glove sample lies this close to the video frame


class Loader(QtCore.QThread):
    loaded = QtCore.Signal(int, int, object)
    failed = QtCore.Signal(int, str)

    def __init__(self, sequence):
        super().__init__()
        self.sequence = sequence
        self.condition = threading.Condition()
        self.pending = None
        self.stopping = False

    def request(self, epoch, index, options):
        with self.condition:
            self.pending = (epoch,index,options)
            self.condition.notify()

    def finish(self):
        with self.condition:
            self.stopping = True
            self.condition.notify()
        self.wait()

    def run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.pending is not None or self.stopping)
                if self.stopping:
                    return
                epoch,index,options = self.pending
                self.pending = None
            try:
                self.loaded.emit(epoch,index,self.sequence.load(index,**options))
            except Exception as error:
                logging.exception("3D frame reconstruction failed")
                self.failed.emit(epoch,str(error))


class DepthPlayer(QtWidgets.QMainWindow):
    glove_loaded = QtCore.Signal(int, object)

    def __init__(self, folder=None):
        super().__init__()
        self.glove_loaded.connect(self.glove_ready)
        self.setWindowTitle("Depth Replay 3D")
        self.resize(1450,920)
        self.setMinimumSize(1080,720)
        self.setWindowIcon(QtGui.QIcon(str(APP_ICON)))
        self.sequence = self.loader = None
        self.current, self.display = {}, {}
        self.current_index = 0
        self.board_mode = False
        self.camera_checks, self.scatters, self.frustums, self.markers = {}, {}, {}, {}
        self.camera_group = None
        self.epoch = 0
        self.requested_index = 0
        self.playing = False
        self.auto_fit = True
        self._build()
        self.timer=QtCore.QTimer(self)
        self.timer.setInterval(20)
        self.timer.timeout.connect(self.play_tick)
        self.timer.start()
        self.seek_timer=QtCore.QTimer(self)
        self.seek_timer.setSingleShot(True)
        self.seek_timer.setInterval(40)
        self.seek_timer.timeout.connect(self.request_frame)
        for key,callback in [("Space",self.toggle_play),("Left",lambda:self.step(-1)),
                             ("Right",lambda:self.step(1)),("Home",lambda:self.seek(0)),
                             ("End",lambda:self.seek(self.sequence.count-1) if self.sequence else None)]:
            shortcut=QtGui.QShortcut(QtGui.QKeySequence(key),self)
            shortcut.activated.connect(callback)
        QtCore.QTimer.singleShot(100,lambda:self.open_folder(folder or latest_recording()))

    def _build(self):
        self.setStyleSheet("""
            QMainWindow,QWidget { background:#0b1118; color:#eaf3fc; font-family:'Malgun Gothic'; font-size:12px; }
            QLabel#brand {font-size:23px;font-weight:700;} QLabel#muted {color:#94a8bb;}
            QLabel#badge {color:#ffd293;background:#31271d;border-radius:7px;padding:10px;}
            QPushButton {background:#1a2a38;border:1px solid #2a4153;border-radius:5px;padding:9px 12px;}
            QPushButton:hover {background:#293f50;} QPushButton:disabled {color:#64778a;}
            QPushButton#primary {background:#6ee7c5;color:#0b1118;font-weight:bold;}
            QSpinBox,QDoubleSpinBox,QComboBox {background:#152431;border:1px solid #304658;border-radius:4px;padding:7px;}
            QTabWidget::pane {border:1px solid #233647;}
            QTabBar::tab {background:#162330;color:#a6b7c7;padding:10px 15px;}
            QTabBar::tab:selected {background:#223747;color:#eef7ff;}
            QSlider::groove:horizontal {height:6px;background:#243747;border-radius:3px;}
            QSlider::handle:horizontal {width:14px;margin:-5px 0;background:#6ee7c5;border-radius:7px;}
            QCheckBox {padding:5px 0;} QCheckBox:disabled {color:#56687a;}
            QGroupBox {border:1px solid #273b4b;border-radius:6px;margin-top:12px;padding-top:15px;}
            QGroupBox::title {subcontrol-origin:margin;left:10px;color:#a8bbcd;}
            QStatusBar {border-top:1px solid #213442;color:#9bb1c4;}
        """)
        central=QtWidgets.QWidget()
        self.setCentralWidget(central)
        main=QtWidgets.QVBoxLayout(central)
        main.setContentsMargins(20,18,20,14)
        header=QtWidgets.QHBoxLayout()
        brand=QtWidgets.QLabel("◉  DEPTH REPLAY 3D")
        brand.setObjectName("brand")
        header.addWidget(brand)
        header.addStretch()
        self.camera_box=QtWidgets.QHBoxLayout()
        self.camera_box.setSpacing(16)
        header.addLayout(self.camera_box)
        header.addSpacing(12)
        self.mode=QtWidgets.QLabel("녹화 로딩 중")
        self.mode.setObjectName("badge")
        header.addWidget(self.mode)
        self.open_button=QtWidgets.QPushButton("녹화 폴더 열기")
        self.open_button.clicked.connect(self.pick_folder)
        header.addWidget(self.open_button)
        main.addLayout(header)
        self.path_label=QtWidgets.QLabel("")
        self.path_label.setObjectName("muted")
        self.path_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        main.addWidget(self.path_label)

        body=QtWidgets.QHBoxLayout()
        main.addLayout(body,1)
        tabs=QtWidgets.QTabWidget()
        tabs.setFixedWidth(290)
        body.addWidget(tabs)
        settings=QtWidgets.QWidget()
        layout=QtWidgets.QVBoxLayout(settings)
        layout.setSpacing(11)
        tabs.addTab(settings,"3D 설정")
        self.explanation=QtWidgets.QLabel("")
        self.explanation.setWordWrap(True)
        self.explanation.setStyleSheet("color:#efc38a;line-height:1.4;")
        layout.addWidget(self.explanation)
        self.range_title=QtWidgets.QLabel("깊이 범위 · mm")
        layout.addWidget(self.range_title)
        bounds=QtWidgets.QHBoxLayout()
        self.near=QtWidgets.QDoubleSpinBox()
        self.far=QtWidgets.QDoubleSpinBox()
        for spin in [self.near,self.far]:
            spin.setRange(1,10000)
            spin.setDecimals(1)
            spin.setSingleStep(10)
            bounds.addWidget(spin)
        layout.addLayout(bounds)
        self.apply=QtWidgets.QPushButton("범위 적용")
        self.apply.clicked.connect(self.reconfigure)
        layout.addWidget(self.apply)
        form=QtWidgets.QFormLayout()
        self.coloring=QtWidgets.QComboBox()
        self.coloring.addItems(["D405 컬러","깊이 색상","카메라별 색상"])
        self.coloring.currentIndexChanged.connect(self.recolor)
        form.addRow("점 색상",self.coloring)
        self.quality=QtWidgets.QComboBox()
        self.quality.addItems(["빠르게 · 4픽셀","기본 · 2픽셀","자세히 · 1픽셀"])
        self.quality.setCurrentIndex(1)
        self.quality.currentIndexChanged.connect(self.reconfigure)
        form.addRow("점 밀도",self.quality)
        self.speed=QtWidgets.QComboBox()
        self.speed.addItems(["0.25×","0.5×","1×","2×"])
        self.speed.setCurrentIndex(2)
        self.speed.currentIndexChanged.connect(self.restart_clock)
        form.addRow("재생 속도",self.speed)
        layout.addLayout(form)
        self.clean=QtWidgets.QCheckBox("가장자리 노이즈 제거")
        self.clean.setChecked(True)
        self.clean.toggled.connect(self.reconfigure)
        layout.addWidget(self.clean)
        self.show_glove=QtWidgets.QCheckBox("MANUS 글러브 3D 패널")
        self.show_glove.setChecked(True)
        self.show_glove.toggled.connect(self.update_glove_visibility)
        layout.addWidget(self.show_glove)
        self.show_grid=QtWidgets.QCheckBox("5cm 격자 / 카메라 위치 표시")
        self.show_grid.setChecked(True)
        self.show_grid.toggled.connect(self.toggle_grid)
        layout.addWidget(self.show_grid)
        sizes=QtWidgets.QHBoxLayout()
        sizes.addWidget(QtWidgets.QLabel("점 크기"))
        self.point_size=QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.point_size.setRange(1,7)
        self.point_size.setValue(2)
        self.point_size.valueChanged.connect(self.recolor)
        sizes.addWidget(self.point_size)
        layout.addLayout(sizes)
        cameras=QtWidgets.QHBoxLayout()
        for name,callback in [("정면",self.front_view),("사선",self.angle_view),("맞춤",self.fit_view)]:
            b=QtWidgets.QPushButton(name)
            b.clicked.connect(callback)
            cameras.addWidget(b)
        layout.addLayout(cameras)
        self.stats=QtWidgets.QLabel("포인트 로딩 중…")
        self.stats.setWordWrap(True)
        layout.addWidget(self.stats)
        layout.addStretch()
        export=QtWidgets.QPushButton("현재 프레임 PLY 저장")
        export.clicked.connect(self.export)
        layout.addWidget(export)
        help_label=QtWidgets.QLabel("왼쪽 드래그: 회전\n가운데 드래그: 이동 · 휠: 확대\n← / →: 한 프레임 이동 · Space: 재생")
        help_label.setObjectName("muted")
        layout.addWidget(help_label)

        references=QtWidgets.QWidget()
        ref_layout=QtWidgets.QVBoxLayout(references)
        tabs.addTab(references,"2D 영상")
        self.thumb_camera=QtWidgets.QComboBox()
        self.thumb_camera.currentIndexChanged.connect(self.update_thumbnails)
        ref_layout.addWidget(self.thumb_camera)
        self.thumbnails=[]
        for title in ["컬러 · 같은 프레임","깊이 영상 · 같은 프레임"]:
            ref_layout.addWidget(QtWidgets.QLabel(title))
            thumb=QtWidgets.QLabel()
            thumb.setFixedSize(260,195)
            thumb.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            thumb.setStyleSheet("background:#060d14;")
            self.thumbnails.append(thumb)
            ref_layout.addWidget(thumb)
        ref_layout.addStretch()

        self.view=gl.GLViewWidget()
        self.view.setBackgroundColor("#080f17")
        self.splitter=QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.view)
        self.splitter.addWidget(self._build_glove_panel())
        self.splitter.setStretchFactor(0,3)
        self.splitter.setStretchFactor(1,1)
        self.splitter.setSizes([780,360])
        self.splitter.splitterMoved.connect(self.splitter_resize_hook)
        body.addWidget(self.splitter,1)
        self.grid=gl.GLGridItem()
        self.grid.setSize(1.5,1.5)
        self.grid.setSpacing(.05,.05)
        self.grid.setColor((75,103,128,100))
        self.view.addItem(self.grid)
        self.axis=gl.GLAxisItem()
        self.axis.setSize(.1,.1,.1)
        self.view.addItem(self.axis)
        self.view.setCameraPosition(distance=1,elevation=20,azimuth=-60)

        controls=QtWidgets.QHBoxLayout()
        self.previous=QtWidgets.QPushButton("◀ 이전")
        self.previous.clicked.connect(lambda:self.step(-1))
        self.play=QtWidgets.QPushButton("▶ 재생")
        self.play.setObjectName("primary")
        self.play.clicked.connect(self.toggle_play)
        self.next=QtWidgets.QPushButton("다음 ▶")
        self.next.clicked.connect(lambda:self.step(1))
        for widget in [self.previous,self.play,self.next]:
            controls.addWidget(widget)
        self.slider=QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.slider.valueChanged.connect(self.slider_changed)
        self.slider.sliderPressed.connect(lambda:self.set_playing(False))
        controls.addWidget(self.slider,1)
        self.frame_spin=QtWidgets.QSpinBox()
        self.frame_spin.setPrefix("프레임 ")
        self.frame_spin.setRange(1,1)
        self.frame_spin.valueChanged.connect(lambda value:self.seek(value-1))
        controls.addWidget(self.frame_spin)
        self.time_label=QtWidgets.QLabel("00:00.000 / 00:00.000")
        controls.addWidget(self.time_label)
        main.addLayout(controls)
        self.statusBar().showMessage("녹화를 읽고 있습니다…")

    def _build_glove_panel(self):
        self.glove_panel=QtWidgets.QWidget()
        layout=QtWidgets.QVBoxLayout(self.glove_panel)
        layout.setContentsMargins(8,0,0,0)
        title=QtWidgets.QLabel("MANUS 글러브 3D · 손목 기준 (트래커 미연동)")
        title.setStyleSheet("font-weight:bold;")
        layout.addWidget(title)
        self.glove_view=gl.GLViewWidget()
        self.glove_view.setBackgroundColor("#080f17")
        self.glove_view.setMinimumWidth(240)
        layout.addWidget(self.glove_view,1)
        grid=gl.GLGridItem()
        grid.setSize(.6,.4)
        grid.setSpacing(.05,.05)
        grid.setColor((75,103,128,90))
        grid.translate(0,0,-.12)
        self.glove_view.addItem(grid)
        self.glove_items={}
        self.glove_offsets={"left":np.array([-GLOVE_OFFSET_M,0,0],np.float32),
                            "right":np.array([GLOVE_OFFSET_M,0,0],np.float32)}
        for side in ("left","right"):
            bones=gl.GLLinePlotItem(pos=np.zeros((2,3),np.float32),mode="lines",width=5,antialias=True)
            joints=gl.GLScatterPlotItem(pos=np.zeros((1,3),np.float32),size=4,pxMode=True,color=(.93,.96,.99,1))
            caption=gl.GLTextItem(pos=(0,0,0),text="왼손" if side=="left" else "오른손",
                                  color=(234,243,252,255),font=QtGui.QFont(FONT,11))
            for item in (bones,joints,caption):
                self.glove_view.addItem(item)
            self.glove_items[side]=(bones,joints,caption)
        self.glove_grid=grid
        self.glove_fit_timer=QtCore.QTimer(self)
        self.glove_fit_timer.setSingleShot(True)
        self.glove_fit_timer.setInterval(120)
        self.glove_fit_timer.timeout.connect(self.fit_glove_view)
        self.splitter_resize_hook=lambda *a:self.glove_fit_timer.start()
        self.glove_view.opts['center']=QtGui.QVector3D(0,0,0)
        self.glove_view.setCameraPosition(distance=.55,elevation=25,azimuth=-90)
        self.glove_status=QtWidgets.QLabel("")
        self.glove_status.setObjectName("muted")
        self.glove_status.setWordWrap(True)
        layout.addWidget(self.glove_status)
        self.glove_data=None
        self.glove_token=0
        self.glove_available=False
        self.glove_panel.setVisible(False)
        return self.glove_panel

    def load_glove(self,folder):
        self.glove_data=None
        self.glove_token+=1
        token=self.glove_token
        path=Path(folder)/"manus.jsonl"
        self.glove_available=path.is_file()
        if not self.glove_available:
            self.glove_status.setText("이 녹화에는 MANUS 글러브 데이터가 없습니다.")
            self.update_glove_visibility()
            return
        self.glove_status.setText("글러브 데이터를 읽는 중…")
        self.update_glove_visibility()

        def work():
            try:
                data=load_manus(folder,("skeleton",))
            except Exception as error:
                logging.exception("MANUS data load failed")
                data=str(error)
            self.glove_loaded.emit(token,data)
        threading.Thread(target=work,daemon=True,name="MANUS-load").start()

    def glove_ready(self,token,data):
        if token!=self.glove_token:
            return  # a different recording was opened meanwhile
        if isinstance(data,str):
            self.glove_status.setText(f"글러브 데이터 읽기 실패: {data}")
            return
        header=data.get("header") or {}
        hands={}
        for glove_id,entry in data["skeleton"].items():
            side=entry["side"] or ("left" if "left" not in hands else "right")
            nodes=entry["nodes"]
            parents,chains=skeleton_layout(header.get("skeleton_nodes",{}).get(str(glove_id)),nodes.shape[1])
            pairs=np.array([(i,p) for i,p in enumerate(parents) if i!=p],dtype=int)
            colors=np.array([QtGui.QColor(FINGER_COLORS.get(chains[i],"#edf5fc")).getRgbF() for i,_ in pairs],np.float32)
            hands[side]=dict(t=entry["t_host_s"],points=nodes[:,:,1:4],pairs=pairs,colors=np.repeat(colors,2,axis=0))
        self.glove_data=hands
        self.update_glove_visibility()
        self.fit_glove_view()

    def update_glove_visibility(self,*args):
        self.show_glove.setEnabled(self.glove_available)
        self.show_glove.setToolTip("" if self.glove_available else "이 녹화에는 manus.jsonl이 없습니다")
        self.glove_panel.setVisible(self.show_glove.isChecked() and self.glove_available)
        self.glove_fit_timer.start()

    def fit_glove_view(self):
        """Stack hands vertically in a tall panel, then frame their range over the whole recording."""
        if not self.glove_data:
            return
        width,height=max(1,self.glove_view.width()),max(1,self.glove_view.height())
        stacked=height>width*1.2
        step=np.array([0,0,GLOVE_OFFSET_M*1.4] if stacked else [GLOVE_OFFSET_M,0,0],np.float32)
        self.glove_offsets={"left":step if stacked else -step,"right":-step if stacked else step}
        samples=[]
        for side,hand in self.glove_data.items():
            if side in self.glove_offsets and len(hand["points"]):
                frames=hand["points"][::max(1,len(hand["points"])//200)]
                samples.append(frames.reshape(-1,3)+self.glove_offsets[side])
        if not samples:
            return
        points=np.concatenate(samples)
        low,high=np.percentile(points,[1,99],axis=0)
        center=(low+high)/2
        extent=np.maximum(high-low,.05)
        # Viewed from the front (azimuth -90): screen x = world X, screen y = world Z.
        aspect=width/height
        fov=np.radians(self.glove_view.opts.get("fov",60))
        visible=2*np.tan(fov/2)
        distance=max(extent[0]/aspect,extent[2])/visible*1.05+extent[1]/2
        self.glove_view.opts["center"]=QtGui.QVector3D(*[float(v) for v in center])
        self.glove_view.setCameraPosition(distance=float(distance),elevation=15,azimuth=-90)
        self.glove_grid.resetTransform()
        self.glove_grid.translate(float(center[0]),float(center[1]),float(low[2]-.02))
        for side,(bones,joints,caption) in self.glove_items.items():
            hand=self.glove_data.get(side)
            if hand is not None and len(hand["points"]):
                top=float(np.percentile(hand["points"][:,:,2],99))
                caption.setData(pos=tuple(float(v) for v in self.glove_offsets[side]+np.array([0,0,top+.03])))
        self.update_glove()

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,"glove_fit_timer"):
            self.glove_fit_timer.start()

    def update_glove(self):
        if not self.glove_data or not self.sequence or not self.glove_panel.isVisible():
            return
        t=self.current_index/self.sequence.fps
        notes=[]
        for side,(bones,joints,caption) in self.glove_items.items():
            hand=self.glove_data.get(side)
            if hand is None or not len(hand["t"]):
                for item in (bones,joints):
                    item.setVisible(False)
                notes.append(f"{'왼손' if side=='left' else '오른손'} 없음")
                continue
            times=hand["t"]
            k=int(np.clip(np.searchsorted(times,t),1,len(times)-1)) if len(times)>1 else 0
            if len(times)>1 and abs(times[k-1]-t)<abs(times[k]-t):
                k-=1
            gap=abs(times[k]-t)*1000
            shown=gap<=GLOVE_MAX_GAP_MS
            for item in (bones,joints):
                item.setVisible(shown)
            if shown:
                points=hand["points"][k]+self.glove_offsets[side]
                segments=points[hand["pairs"].ravel()]
                bones.setData(pos=segments,color=hand["colors"])
                joints.setData(pos=points)
            notes.append(f"{'왼손' if side=='left' else '오른손'} Δ{gap:.0f} ms" if shown else
                         f"{'왼손' if side=='left' else '오른손'} 샘플 없음 (Δ{gap:.0f} ms)")
        self.glove_status.setText(f"영상 시각 {t:.3f} s · " + " · ".join(notes) + "\n드래그 회전 · 휠 확대")

    def pick_folder(self):
        folder=QtWidgets.QFileDialog.getExistingDirectory(self,"녹화 세션 폴더 선택",str(RECORDINGS))
        if folder:
            self.open_folder(folder)

    def open_folder(self,folder):
        try:
            sequence=MultiSequence(folder)
            calibration=sequence.calibration
            self.set_playing(False)
            if self.loader:
                self.loader.finish()
                self.sequence.close()
            self.sequence=sequence
            self.current,self.display={},{}
            self.epoch+=1
            self.auto_fit=True
            self.board_mode=bool(sequence.calibrated)
            self.loader=Loader(sequence)
            self.loader.loaded.connect(self.loaded)
            self.loader.failed.connect(self.failed)
            self.loader.start()
            self.build_camera_items(calibration)
            self.load_glove(sequence.folder)
            self.path_label.setText(str(sequence.folder))
            self.setWindowTitle(f"Depth Replay 3D | {sequence.folder.name}")
            approximate=any(item.approximate for item in sequence.sequences.values())
            depth_mode="근사 3D · 색상 영상 역산" if approximate else "원본 깊이 · 16비트 무손실"
            self.mode.setText(depth_mode+(" · 보드 좌표계" if self.board_mode else " · 카메라 좌표계"))
            notes=[]
            if self.board_mode:
                created=calibration.get("created_at","")[:16].replace("T"," ")
                origin=CALIBRATION_ORIGINS.get(sequence.calibration_origin,"지정한 보정")
                source=sequence.calibration_path
                where=f"{source.parent.name}/{source.name}" if source else "calibration.json"
                notes.append(f"외부 보정: {origin} · {where} ({created}). 세 카메라를 보드 좌표계(Z 위쪽)에 겹쳐 표시합니다. "
                             "카메라 간 동기화는 하드웨어가 아닌 컴퓨터 시계 기준입니다.")
                if sequence.calibration_origin=="active":
                    notes.append("⚠ 이 녹화 이전에 만든 보정을 찾지 못해 현재 보정을 사용합니다. 카메라를 옮긴 뒤라면 구름이 어긋납니다.")
                missing=[self.camera_checks[key].text() for key in sequence.camera_keys if key not in sequence.calibrated]
                if missing:
                    notes.append(f"보정 데이터에 S/N이 없어 제외: {', '.join(missing)}")
            else:
                notes.append("이 녹화의 카메라 S/N과 맞는 보정 파일이 없어 한 번에 한 카메라만 카메라 좌표계로 표시합니다.")
            if approximate:
                notes.append("원본 깊이값이 없는 트랙은 색상을 깊이로 역산한 근사값이며, 검정 영역은 복원할 수 없습니다.")
            self.explanation.setText("\n".join(notes))
            self.range_title.setText("녹화 표시 범위 가정 · mm (미기록)" if approximate and not all(
                item.range_recorded for item in sequence.sequences.values()) else "깊이 범위 · mm (모든 카메라 공통)")
            self.apply_default_range()
            self.slider.blockSignals(True)
            self.slider.setRange(0,sequence.count-1)
            self.slider.setValue(0)
            self.slider.blockSignals(False)
            self.frame_spin.blockSignals(True)
            self.frame_spin.setRange(1,sequence.count)
            self.frame_spin.setSuffix(f" / {sequence.count}")
            self.frame_spin.setValue(1)
            self.frame_spin.blockSignals(False)
            self.seek(0)
        except Exception as error:
            logging.exception("3D recording open failed")
            self.statusBar().showMessage(str(error))

    def build_camera_items(self,calibration):
        for item in [*self.scatters.values(),*self.frustums.values(),*self.markers.values()]:
            self.view.removeItem(item)
        while self.camera_box.count():
            widget=self.camera_box.takeAt(0).widget()
            if widget:
                widget.deleteLater()
        self.camera_checks,self.scatters,self.frustums,self.markers={},{},{},{}
        self.camera_group=QtWidgets.QButtonGroup(self)
        self.camera_group.setExclusive(not self.board_mode)
        self.thumb_camera.blockSignals(True)
        self.thumb_camera.clear()
        for index,key in enumerate(self.sequence.camera_keys):
            color=CAMERA_COLORS[index%len(CAMERA_COLORS)]
            serial=self.sequence.sequences[key].info.get("serial",key)
            check=QtWidgets.QCheckBox(f"RGB-D {index+1}")
            check.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
            hex_color="#%02x%02x%02x"%tuple(int(c*255) for c in color)
            check.setStyleSheet(f"QCheckBox {{color:{hex_color};font-weight:bold;}} QCheckBox:disabled {{color:#56687a;}}")
            calibrated=key in self.sequence.calibrated
            if self.board_mode and not calibrated:
                check.setEnabled(False)
                check.setToolTip(f"S/N {serial} · 보정 데이터에 없음")
            else:
                check.setToolTip(f"S/N {serial}")
            check.setChecked(calibrated if self.board_mode else index==0)
            check.toggled.connect(self.cameras_changed)
            self.camera_group.addButton(check)
            self.camera_box.addWidget(check)
            self.camera_checks[key]=check
            scatter=gl.GLScatterPlotItem(pos=np.empty((0,3),np.float32),size=2,pxMode=True,glOptions="opaque")
            self.view.addItem(scatter)
            self.scatters[key]=scatter
            frustum=gl.GLLinePlotItem(pos=np.zeros((2,3),np.float32),color=(*color,.7),mode="lines",width=1,antialias=True)
            marker=gl.GLScatterPlotItem(pos=np.zeros((1,3),np.float32),color=(*color,1),size=9,pxMode=True)
            for item in (frustum,marker):
                item.setVisible(False)
                self.view.addItem(item)
            self.frustums[key],self.markers[key]=frustum,marker
            self.thumb_camera.addItem(f"RGB-D {index+1} · {serial}",key)
        self.thumb_camera.blockSignals(False)

    def selected_keys(self):
        return [key for key,check in self.camera_checks.items() if check.isChecked() and check.isEnabled()]

    def apply_default_range(self):
        keys=self.selected_keys() if not self.board_mode else self.sequence.calibrated
        ranges=[self.sequence.sequences[key].assumed_range for key in keys] or [[.07,.5]]
        self.near.setValue(min(r[0] for r in ranges)*1000)
        self.far.setValue(max(r[1] for r in ranges)*1000)

    def cameras_changed(self,checked):
        if not self.sequence:
            return
        if not self.board_mode:
            if not checked:
                return  # exclusive group: the newly checked camera drives the update
            self.current,self.display={},{}
            self.auto_fit=True
            self.apply_default_range()
            index=self.thumb_camera.findData(self.selected_keys()[0])
            self.thumb_camera.setCurrentIndex(index)
        self.recolor()
        self.update_frustums()
        self.epoch+=1
        self.seek_timer.start()

    def options(self):
        near,far=self.near.value()/1000,self.far.value()/1000
        if near>=far:
            raise ValueError("가까운 거리는 먼 거리보다 작아야 합니다.")
        return dict(keys=self.selected_keys(),stride=[4,2,1][self.quality.currentIndex()],
                    near=near,far=far,clean=self.clean.isChecked())

    def request_frame(self):
        if not self.sequence:
            return
        try:
            options=self.options()
            if not options["keys"]:
                self.stats.setText("표시할 카메라를 위쪽에서 선택하세요.")
                return
            self.loader.request(self.epoch,self.requested_index,options)
            self.statusBar().showMessage(f"프레임 {self.requested_index+1:,} 읽는 중…")
        except ValueError as error:
            self.statusBar().showMessage(str(error))

    def seek(self,index):
        if not self.sequence:
            return
        self.requested_index=max(0,min(self.sequence.count-1,int(index)))
        for widget,value in [(self.slider,self.requested_index),(self.frame_spin,self.requested_index+1)]:
            widget.blockSignals(True)
            widget.setValue(value)
            widget.blockSignals(False)
        self.seek_timer.start()

    def slider_changed(self,index):
        self.set_playing(False)
        self.seek(index)

    def step(self,delta):
        self.set_playing(False)
        self.seek(self.requested_index+delta)

    def reconfigure(self,*args):
        if self.sequence:
            self.epoch+=1
            self.request_frame()

    def set_playing(self,playing):
        self.playing=bool(playing and self.sequence)
        self.play.setText("Ⅱ 일시 정지" if self.playing else "▶ 재생")
        if self.playing:
            self.restart_clock()

    def toggle_play(self):
        if self.sequence and not self.playing and self.requested_index==self.sequence.count-1:
            self.seek(0)
        self.set_playing(not self.playing)

    def restart_clock(self,*args):
        self.play_start=time.monotonic()
        self.play_start_frame=self.requested_index

    def play_tick(self):
        if not self.playing or not self.sequence:
            return
        speed=[.25,.5,1,2][self.speed.currentIndex()]
        target=self.play_start_frame+int((time.monotonic()-self.play_start)*self.sequence.fps*speed)
        if target>=self.sequence.count-1:
            target=self.sequence.count-1
            self.set_playing(False)
        if target!=self.requested_index:
            self.seek(target)
            # Playback schedules immediately; slider scrubbing keeps its debounce.
            self.seek_timer.stop()
            self.request_frame()

    def to_display(self,key,points):
        """Board frame (Z up) when calibrated, else camera frame turned so Z is up."""
        if self.board_mode:
            transform=self.sequence.transforms[key]
            return np.ascontiguousarray(points@transform[:3,:3].T+transform[:3,3],dtype=np.float32)
        result=points[:,[0,2,1]].copy()
        result[:,2]*=-1
        return np.ascontiguousarray(result,dtype=np.float32)

    def shown_keys(self):
        return [key for key in self.selected_keys() if key in self.current]

    def merged_points(self):
        keys=self.shown_keys()
        return np.concatenate([self.display[key] for key in keys]) if keys else np.empty((0,3),np.float32)

    def loaded(self,epoch,index,frames):
        if epoch!=self.epoch:
            return
        self.current=frames
        self.current_index=index
        self.display={key:self.to_display(key,frame.points) for key,frame in frames.items()}
        self.recolor()
        points=self.merged_points()
        if self.auto_fit and len(points):
            self.fit_view()
            self.auto_fit=False
        self.update_frustums()
        self.update_thumbnails()
        self.update_glove()
        def stamp(seconds):
            return f"{int(seconds)//60:02}:{seconds%60:06.3f}"
        self.time_label.setText(f"{stamp(index/self.sequence.fps)} / {stamp((self.sequence.count-1)/self.sequence.fps)}")
        if len(points):
            extent=np.ptp(points,axis=0)*1000
            per_camera="  ·  ".join(f"{self.camera_checks[key].text()} {len(self.current[key].points):,}" for key in self.shown_keys())
            depth_range=[(frame.min_z,frame.max_z) for frame in frames.values() if len(frame.points)]
            self.stats.setText(f"{len(points):,}개 포인트\n{per_camera}\n"
                               f"카메라 거리 {min(r[0] for r in depth_range)*1000:.0f}–{max(r[1] for r in depth_range)*1000:.0f} mm\n"
                               f"X / Y / Z 범위 {extent[0]:.0f} / {extent[1]:.0f} / {extent[2]:.0f} mm")
        else:
            self.stats.setText("복원할 수 있는 포인트가 없습니다.")
        prefix="근사 복원" if any(frame.approximate for frame in frames.values()) else "원본 깊이 복원"
        self.statusBar().showMessage(f"{prefix} · 프레임 {index+1:,} / {self.sequence.count:,} · 격자 한 칸 50 mm")

    def failed(self,epoch,message):
        if epoch==self.epoch:
            self.set_playing(False)
            self.statusBar().showMessage(message)

    def recolor(self,*args):
        selected=set(self.selected_keys())
        for index,(key,scatter) in enumerate(self.scatters.items()):
            frame=self.current.get(key)
            shown=frame is not None and key in selected
            scatter.setVisible(shown)
            if not shown:
                continue
            mode=self.coloring.currentIndex()
            if mode==2:
                color=(*CAMERA_COLORS[index%len(CAMERA_COLORS)],1.)
            else:
                rgb=frame.rgb_colors if mode==0 else frame.depth_colors
                color=np.ones((len(rgb),4),dtype=np.float32)
                color[:,:3]=rgb
            scatter.setData(pos=self.display[key],color=color,size=self.point_size.value())

    def update_frustums(self):
        if not self.sequence:
            return
        selected=set(self.selected_keys())
        for key in self.frustums:
            rays=self.sequence.sequences[key].rays
            visible=rays is not None and key in selected and self.show_grid.isChecked()
            self.frustums[key].setVisible(visible)
            self.markers[key].setVisible(visible)
            if rays is None:
                continue
            corners=np.array([rays[0,0],rays[0,-1],rays[-1,-1],rays[-1,0]],dtype=np.float32)
            if self.board_mode:
                transform=self.sequence.transforms[key]
                origin=transform[:3,3]
                corners=(corners*BOARD_FRUSTUM_M)@transform[:3,:3].T+origin
            else:
                origin=np.zeros(3,np.float32)
                corners=self.to_display(key,corners*self.far.value()/1000)
            lines=[]
            for i in range(4):
                lines.extend([origin,corners[i],corners[i],corners[(i+1)%4]])
            self.frustums[key].setData(pos=np.asarray(lines,np.float32))
            self.markers[key].setData(pos=np.asarray([origin],np.float32))

    def update_thumbnails(self,*args):
        frame=self.current.get(self.thumb_camera.currentData())
        for widget,array in zip(self.thumbnails,[frame.color_image,frame.depth_image] if frame else [None,None]):
            if array is None:
                widget.clear()
                widget.setText("이 카메라는 현재 표시하지 않습니다")
                continue
            image=QtGui.QImage(array.data,array.shape[1],array.shape[0],array.strides[0],QtGui.QImage.Format.Format_RGB888)
            widget.setPixmap(QtGui.QPixmap.fromImage(image).scaled(widget.size(),QtCore.Qt.AspectRatioMode.KeepAspectRatio,QtCore.Qt.TransformationMode.SmoothTransformation))

    def fit_view(self):
        points=self.merged_points()
        if not len(points):
            return
        center=np.median(points,axis=0)
        low,high=np.percentile(points,[2,98],axis=0)
        distance=max(.2,float(np.linalg.norm(high-low))*1.7)
        self.view.opts['center']=QtGui.QVector3D(*[float(n) for n in center])
        self.view.setCameraPosition(distance=distance,elevation=20,azimuth=-60)
        self.grid.resetTransform()
        # In the board frame the grid lies on the board plane (Z = 0).
        self.grid.translate(float(center[0]),float(center[1]),0. if self.board_mode else float(low[2]-.025))
        self.update_frustums()

    def front_view(self):
        self.view.setCameraPosition(elevation=0,azimuth=-90)

    def angle_view(self):
        self.view.setCameraPosition(elevation=20,azimuth=-60)

    def toggle_grid(self,checked):
        for item in [self.grid,self.axis]:
            item.setVisible(checked)
        self.update_frustums()

    def export(self):
        keys=self.shown_keys()
        if not keys:
            return
        target=self.sequence.folder/"reconstruction"
        target.mkdir(exist_ok=True)
        frames=[self.current[key] for key in keys]
        approximate=any(frame.approximate for frame in frames)
        mode="approx" if approximate else "raw"
        if self.board_mode:
            frame=CloudFrame(self.current_index,self.merged_points(),
                             np.concatenate([f.rgb_colors for f in frames]),
                             np.concatenate([f.depth_colors for f in frames]),
                             frames[0].color_image,frames[0].depth_image,approximate,
                             min(f.min_z for f in frames),max(f.max_z for f in frames))
            name=f"{'+'.join(keys)}_frame_{self.current_index:06d}_{mode}_board.ply"
            created=(self.sequence.calibration or {}).get("created_at","unknown")
            coordinates=(f"meters; ChArUco board frame (calibration created {created}, "
                         f"{self.sequence.calibration_origin or 'given'}); X columns, Y opposite rows, Z above board")
        else:
            frame=frames[0]
            name=f"{keys[0]}_frame_{self.current_index:06d}_{mode}.ply"
            coordinates="meters; X right, Y down, Z forward"
        path,_=QtWidgets.QFileDialog.getSaveFileName(self,"현재 프레임 포인트클라우드 저장",str(target/name),"Point Cloud (*.ply)")
        if path:
            try:
                write_ply(path,frame,self.coloring.currentIndex()==1,
                          [self.near.value()/1000,self.far.value()/1000] if approximate else None,coordinates)
                self.statusBar().showMessage(f"PLY 저장 완료 · {path}")
            except OSError as error:
                self.statusBar().showMessage(str(error))

    def closeEvent(self,event):
        self.timer.stop()
        self.seek_timer.stop()
        if self.loader:
            self.loader.finish()
            self.sequence.close()
        event.accept()


def main():
    parser=argparse.ArgumentParser(description="3D replay of a Camera Studio recording")
    parser.add_argument("folder",nargs="?",help="session folder (default: latest complete recording)")
    args=parser.parse_args()
    configure_logging("depth_player")
    app=QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName("Depth Replay 3D")
    app.setFont(QtGui.QFont(FONT,10))
    window=DepthPlayer(args.folder)
    window.show()
    sys.exit(app.exec())


if __name__=="__main__":
    main()
