"""Camera Studio: three RealSense RGB-D cameras, MANUS gloves and VIVE trackers in one recorder."""
from __future__ import annotations

from datetime import datetime
from functools import partial
import json
import logging
import os
from pathlib import Path
import queue
from statistics import median
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ..calibration.board import (BOARD_SPEC, board_spec, calibrate, load_calibration, verify_capture,
                                 verify_snapshot)
from ..calibration.capture import capture as capture_calibration_frames
from ..calibration.scale import fit_scale
from ..cameras.d405 import LUT, distance_mm
from ..cameras.extra import GROUPS as EXTRA_GROUPS, STREAM_TITLES as EXTRA_TITLES, ExtraCameras
from ..cameras.hub import CAMERA_KEYS, CAMERA_NAMES, STREAM_NAMES, CameraHub
from ..cameras.recorder import Recorder
from ..devices.manus import ManusGlove
from ..devices.vive import ViveTracker
from ..paths import ACTIVE_CALIBRATION, APP_ICON, CALIBRATIONS, LOGS, RECORDINGS, REPO_ROOT, configure_logging
from ..settings import read_settings, write_settings
from .theme import ACCENT, AMBER, BG, CARD, FONT, LINE, MUTED, PANEL, RED, TEXT
from .widgets import GlovePanel, Preview, button, label


class Viewer(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Camera Studio | RGB-D + 4K RGB")
        self.configure(bg=BG)
        self.geometry(f"{min(1600,self.winfo_screenwidth()-80)}x{min(950,self.winfo_screenheight()-90)}")
        self.minsize(1160,780)
        if APP_ICON.exists():
            self.iconbitmap(str(APP_ICON))
        self.settings = read_settings()
        self.hub = CameraHub()
        self.recorder = Recorder(self.hub)
        self.glove = ManusGlove()
        self.tracker = ViveTracker()
        self.extras = ExtraCameras()
        self.camera_group = self.settings.get("camera_group", "d405")
        if self.camera_group not in ("d405", *EXTRA_GROUPS):
            self.camera_group = "d405"
        self.extra_errors = {}
        self.output_dir = Path(self.settings.get("recording_directory", str(RECORDINGS)))
        self.points = {key: (320, 240) for key in CAMERA_KEYS}
        self.selected_camera = CAMERA_KEYS[0]
        self.focused = None
        self.closing = self.saving = self.calibrating = False
        self.close_started = None
        self.ui_events = queue.SimpleQueue()
        self.camera_errors = {}
        self.choice_serials = {}
        self.calibration = load_calibration()
        self.show_calibration = tk.BooleanVar(value=bool(self.calibration))
        self.validation = self.calibration.get("normal_resolution_validation") if self.calibration else None
        self._build()
        self.protocol("WM_DELETE_WINDOW",self.close)
        self.bind("<F11>",lambda e:self.attributes("-fullscreen",not bool(self.attributes("-fullscreen"))))
        self.bind("<Escape>",lambda e:self.escape())
        self.bind("<Control-s>",lambda e:self.snapshot())
        self.bind("<Control-r>",lambda e:self.toggle_recording())
        self.bind("<space>",lambda e:self.toggle_connection() if e.widget.winfo_class() not in
                  {"Entry","Spinbox","TEntry","TSpinbox","TCombobox"} else None)
        self.after(200,self.hub.start)
        self.after(400,self.glove.start)
        if self.settings.get("vive_autoconnect", True):
            self.after(600,self.tracker.start)
        if self.camera_group != "d405":
            self.after(800, lambda: self.extras.activate(self.camera_group))
        self.after(70,self.tick)

    def _build(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TNotebook",background=BG,borderwidth=0)
        style.configure("TNotebook.Tab",background=CARD,foreground=MUTED,padding=(10,9),font=(FONT,9))
        style.map("TNotebook.Tab",background=[("selected",PANEL)],foreground=[("selected",TEXT)])
        style.configure("TCombobox",fieldbackground=CARD,foreground=TEXT,background=LINE,arrowcolor=TEXT,padding=6)
        style.map("TCombobox",fieldbackground=[("readonly",CARD)],foreground=[("readonly",TEXT)])
        header = tk.Frame(self,bg=BG)
        header.pack(fill="x",padx=22,pady=(18,14))
        label(header,"◉",28,ACCENT).pack(side="left",padx=(0,12))
        brand = tk.Frame(header,bg=BG)
        brand.pack(side="left")
        label(brand,"CAMERA STUDIO",20,bold=True).pack(anchor="w")
        label(brand,"RealSense D405 × 3 동시 캡처  /  D435 · 4K RGB 미리보기",9,MUTED).pack(anchor="w",pady=(3,0))
        self.top_status = label(header,"●  카메라 연결 준비",11,MUTED)
        self.top_status.pack(side="right")

        body = tk.Frame(self,bg=BG)
        body.pack(fill="both",expand=True,padx=22)
        sidebar = tk.Frame(body,bg=BG,width=268)
        sidebar.pack(side="left",fill="y",padx=(0,16))
        sidebar.pack_propagate(False)
        label(sidebar,"CAMERAS",9,MUTED,True).pack(anchor="w",pady=(0,8))
        self.device_labels = {}
        self.device_details = {}
        for key, title in CAMERA_NAMES.items():
            row = tk.Frame(sidebar,bg=PANEL)
            row.pack(fill="x",pady=(0,4))
            label(row,title,10,bold=True,width=7,anchor="w").pack(side="left",padx=10,pady=7)
            detail = tk.Frame(row, bg=PANEL)
            detail.pack(side="right", padx=10, pady=4)
            self.device_labels[key] = label(row,"연결 대기",9,MUTED)
            self.device_labels[key].configure(bg=PANEL)
            self.device_labels[key].pack(in_=detail, anchor="e")
            self.device_details[key] = label(detail, "S/N —", 8, MUTED)
            self.device_details[key].pack(anchor="e")
        glove_row = tk.Frame(sidebar,bg=PANEL)
        glove_row.pack(fill="x",pady=(0,4))
        label(glove_row,"글러브",10,bold=True,width=7,anchor="w").pack(side="left",padx=10,pady=7)
        glove_detail = tk.Frame(glove_row,bg=PANEL)
        glove_detail.pack(side="right",padx=10,pady=4)
        self.glove_label = label(glove_detail,"MANUS 대기",9,MUTED)
        self.glove_label.pack(anchor="e")
        self.glove_detail = label(glove_detail,"",8,MUTED,wraplength=150,justify="right")
        self.glove_detail.pack(anchor="e")
        for widget in (glove_row, glove_detail, self.glove_label, self.glove_detail):
            widget.bind("<Button-1>", self.retry_glove)
        tracker_row = tk.Frame(sidebar,bg=PANEL,cursor="hand2")
        tracker_row.pack(fill="x",pady=(0,4))
        label(tracker_row,"트래커",10,bold=True,width=7,anchor="w").pack(side="left",padx=10,pady=7)
        tracker_detail = tk.Frame(tracker_row,bg=PANEL)
        tracker_detail.pack(side="right",padx=10,pady=4)
        self.tracker_label = label(tracker_detail,"VIVE 대기",9,MUTED,wraplength=170,justify="right")
        self.tracker_label.pack(anchor="e")
        self.tracker_detail = label(tracker_detail,"",8,MUTED,wraplength=170,justify="right")
        self.tracker_detail.pack(anchor="e")
        for widget in (tracker_row, tracker_detail, self.tracker_label, self.tracker_detail):
            widget.bind("<Button-1>", self.toggle_tracker)
        self.connect_button = button(sidebar,"전체 연결 종료",self.toggle_connection)
        self.connect_button.pack(fill="x",pady=(6,14))

        tabs = ttk.Notebook(sidebar)
        tabs.pack(fill="both",expand=True)
        rec_tab,device_tab = tk.Frame(tabs,bg=PANEL),tk.Frame(tabs,bg=PANEL)
        tabs.add(rec_tab,text="녹화")
        tabs.add(device_tab,text="장치·깊이")
        calibration_tab = tk.Frame(tabs, bg=PANEL)
        tabs.add(calibration_tab, text="보정·검증")
        calibration_panel = self.scrollable(calibration_tab)
        self.board_title = label(calibration_panel, "", 10, bold=True)
        self.board_title.pack(anchor="w")
        self.board_detail = label(calibration_panel, "", 8, MUTED)
        self.board_detail.pack(anchor="w", pady=(4,6))
        tk.Checkbutton(calibration_panel, text="실시간 Real / Sim 오버레이", variable=self.show_calibration,
                       bg=PANEL, fg=TEXT, selectcolor=CARD, activebackground=PANEL,
                       activeforeground=TEXT, font=(FONT,9)).pack(anchor="w")
        self.calibration_status = label(calibration_panel, "", 8, MUTED, justify="left", wraplength=230)
        self.calibration_status.pack(anchor="w", pady=(4,6))
        self.calibration_metrics = {}
        for key in CAMERA_KEYS:
            self.calibration_metrics[key] = label(calibration_panel, "", 8, TEXT, justify="left")
            self.calibration_metrics[key].pack(anchor="w", pady=(0,5))
        verify_row = tk.Frame(calibration_panel, bg=PANEL)
        verify_row.pack(fill="x", pady=(4,0))
        self.verify_button = button(verify_row, "프레임 검증", self.verify_calibration)
        self.verify_button.pack(side="left", fill="x", expand=True, padx=(0,4))
        self.overlay3d_button = button(verify_row, "3D 보기", self.open_calibration_3d)
        self.overlay3d_button.pack(side="left", fill="x", expand=True)
        button(calibration_panel, "보정 결과 폴더", self.open_calibrations).pack(fill="x", pady=(4,0))

        tk.Frame(calibration_panel, bg=LINE, height=1).pack(fill="x", pady=10)
        label(calibration_panel, "새 외부 보정 · 실측 보드 크기", 9, bold=True).pack(anchor="w")
        nominal = (self.calibration or {}).get("nominal_board") or (self.calibration or {}).get("board") or BOARD_SPEC
        saved = self.settings.get("calibration_square_mm", nominal["square_m"]*1000)
        self.square_var = tk.StringVar(value=f"{float(saved):g}")
        label(calibration_panel, "사각형 한 변 mm", 8, MUTED).pack(anchor="w", pady=(6,0))
        entry = tk.Entry(calibration_panel, textvariable=self.square_var, width=8, font=(FONT,10), bg=CARD, fg=TEXT,
                         insertbackground=TEXT, relief="flat", disabledbackground=CARD)
        entry.pack(fill="x", ipady=5, pady=(3,3))
        self.board_entries = [entry]
        self.marker_preview = label(calibration_panel, "", 8, MUTED)
        self.marker_preview.pack(anchor="w", pady=(0,4))

        def preview_marker(*_):
            try:
                spec = board_spec(self.square_var.get())
                self.marker_preview.configure(text=f"마커 {spec['marker_m']*1000:.2f} mm · 비율 25 : 18 고정", fg=MUTED)
            except ValueError as error:
                self.marker_preview.configure(text=str(error) if "mm" in str(error) else "숫자로 입력하세요.", fg=RED)

        self.square_var.trace_add("write", preview_marker)
        preview_marker()
        self.fit_depth_scale = tk.BooleanVar(value=bool(self.settings.get("calibration_fit_depth_scale", False)))
        self.fit_depth_check = tk.Checkbutton(calibration_panel, text="Depth 기준 스케일 추가 보정", variable=self.fit_depth_scale,
                                              bg=PANEL, fg=TEXT, selectcolor=CARD, activebackground=PANEL,
                                              activeforeground=TEXT, font=(FONT,8))
        self.fit_depth_check.pack(anchor="w")
        self.calibrate_button = button(calibration_panel, "외부 보정 실행", self.run_calibration, True)
        self.calibrate_button.pack(fill="x", pady=(4,0))
        self.update_calibration_metrics()
        rec = tk.Frame(rec_tab,bg=PANEL)
        rec.pack(fill="both",expand=True,padx=13,pady=12)
        label(rec,"저장할 영상",10,bold=True).pack(anchor="w",pady=(0,7))
        self.selections, self.checks = {},[]
        for camera_key, camera_name in CAMERA_NAMES.items():
            row = tk.Frame(rec, bg=PANEL)
            row.pack(fill="x", pady=3)
            label(row, camera_name, 9, bold=True, width=7, anchor="w").pack(side="left")
            for kind, caption in [("color", "컬러"), ("depth", "깊이+원본")]:
                key = f"{camera_key}_{kind}"
                var = tk.BooleanVar(value=key in self.settings.get("selected_streams", list(STREAM_NAMES)))
                check = tk.Checkbutton(row, text=caption, variable=var, bg=PANEL, fg=TEXT,
                                       selectcolor=CARD, activebackground=PANEL, activeforeground=TEXT,
                                       font=(FONT,9), anchor="w", borderwidth=0, padx=2)
                check.pack(side="left")
                self.selections[key] = var
                self.checks.append(check)
        self.record_glove = tk.BooleanVar(value=bool(self.settings.get("manus_record", True)))
        self.glove_check = tk.Checkbutton(rec, text="MANUS 글러브 함께 녹화", variable=self.record_glove,
                                          command=self.persist, bg=PANEL, fg=TEXT, selectcolor=CARD,
                                          activebackground=PANEL, activeforeground=TEXT, font=(FONT,9),
                                          anchor="w", borderwidth=0, padx=2)
        self.glove_check.pack(anchor="w", pady=(6,0))
        self.checks.append(self.glove_check)
        self.record_tracker = tk.BooleanVar(value=bool(self.settings.get("vive_record", True)))
        self.tracker_check = tk.Checkbutton(rec, text="VIVE 트래커 함께 녹화", variable=self.record_tracker,
                                            command=self.persist, bg=PANEL, fg=TEXT, selectcolor=CARD,
                                            activebackground=PANEL, activeforeground=TEXT, font=(FONT,9),
                                            anchor="w", borderwidth=0, padx=2)
        self.tracker_check.pack(anchor="w")
        self.checks.append(self.tracker_check)
        label(rec,"H.264 MP4  ·  원본 해상도  ·  무음",8,MUTED).pack(anchor="w",pady=(9,4))
        label(rec,"깊이 MP4 + 16비트 원본 깊이 저장",8,MUTED).pack(anchor="w",pady=(0,10))
        self.record_button = button(rec,"●  녹화 시작   Ctrl+R",self.toggle_recording,True)
        self.record_button.pack(fill="x")
        self.timer = label(rec,"00:00:00",26,ACCENT,True)
        self.timer.pack(pady=(8,2))
        self.record_state = label(rec,"녹화 준비",9,MUTED)
        self.record_state.pack(pady=(0,12))
        tk.Frame(rec,bg=LINE,height=1).pack(fill="x",pady=(0,12))
        label(rec,"동영상 저장 위치",9,MUTED).pack(anchor="w")
        self.folder_label = label(rec,str(self.output_dir),8,MUTED,wraplength=235,justify="left",anchor="w")
        self.folder_label.pack(fill="x",pady=(5,8))
        folders = tk.Frame(rec,bg=PANEL)
        folders.pack(fill="x")
        self.folder_button = button(folders,"위치 변경",self.choose_folder)
        self.folder_button.pack(side="left",fill="x",expand=True,padx=(0,4))
        button(folders,"폴더 열기",self.open_folder).pack(side="left",fill="x",expand=True)
        tools = tk.Frame(rec,bg=PANEL)
        tools.pack(fill="x",pady=(12,0))
        self.snapshot_button = button(tools,"스냅샷 저장",self.snapshot)
        self.snapshot_button.pack(side="left",fill="x",expand=True,padx=(0,4))
        button(tools,"3D 재생",self.open_3d).pack(side="left",fill="x",expand=True)

        devices = tk.Frame(device_tab,bg=PANEL)
        devices.pack(fill="both",expand=True,padx=13,pady=12)
        label(devices,"RGB-D 카메라 배치",10,bold=True).pack(anchor="w",pady=(0,7))
        self.combos=[]
        for i in range(3):
            label(devices,f"RGB-D {i+1}",9,MUTED).pack(anchor="w",pady=(5,3))
            combo = ttk.Combobox(devices,state="readonly",font=(FONT,9))
            combo.pack(fill="x")
            self.combos.append(combo)
        self.reassign_button = button(devices,"장치 다시 검색 / 배치 적용",self.reassign)
        self.reassign_button.pack(fill="x",pady=(12,4))
        label(devices,"배치 변경은 전체 연결 종료 후 적용",8,MUTED).pack(anchor="w")
        tk.Frame(devices,bg=LINE,height=1).pack(fill="x",pady=12)
        label(devices,"깊이 설정 카메라",9,MUTED).pack(anchor="w")
        self.depth_camera = ttk.Combobox(devices, values=list(CAMERA_NAMES.values()), state="readonly", font=(FONT,9))
        self.depth_camera.current(0)
        self.depth_camera.pack(fill="x", pady=(4,8))
        self.depth_camera.bind("<<ComboboxSelected>>", self.select_depth_camera)
        label(devices,"깊이 표시 범위 · mm",10,bold=True).pack(anchor="w")
        inputs = tk.Frame(devices,bg=PANEL)
        inputs.pack(fill="x",pady=(10,8))
        self.near_var = tk.StringVar(value=str(self.settings.get("near_mm",70)))
        self.far_var = tk.StringVar(value=str(self.settings.get("far_mm",500)))
        for i,(name,var) in enumerate([("가까움",self.near_var),("멀어짐",self.far_var)]):
            cell = tk.Frame(inputs,bg=PANEL)
            cell.pack(side="left",fill="x",expand=True,padx=(0,8 if i==0 else 0))
            label(cell,name,8,MUTED).pack(anchor="w")
            tk.Entry(cell,textvariable=var,width=8,font=(FONT,10),bg=CARD,fg=TEXT,
                     insertbackground=TEXT,relief="flat").pack(fill="x",ipady=7,pady=(4,0))
        button(devices,"표시 범위 적용",self.apply_range).pack(fill="x")
        legend=tk.Canvas(devices,height=10,bg=PANEL,highlightthickness=0)
        legend.pack(fill="x",pady=(10,6))
        for i,rgb in enumerate(LUT):
            legend.create_line(i*240/256,0,i*240/256,10,fill="#%02x%02x%02x"%tuple(rgb))
        label(devices,"범위 밖·측정 불가는 검정",8,MUTED).pack(anchor="w")
        label(devices,"영상을 클릭해 카메라와 측정 지점 선택",8,MUTED).pack(anchor="w",pady=(10,4))
        self.depth_distance=label(devices,"— mm",25,ACCENT,True)
        self.depth_distance.pack(anchor="w")
        self.depth_details=label(devices,"픽셀 (320, 240) · 5×5 중앙값",8,MUTED)
        self.depth_details.pack(anchor="w",pady=(5,0))
        self.apply_range(silent=True)

        content = tk.Frame(body,bg=BG)
        content.pack(side="left",fill="both",expand=True)
        top=tk.Frame(content,bg=BG)
        top.pack(fill="x",pady=(0,8))
        label(top,"LIVE MONITOR",9,MUTED,True).pack(side="left")
        groups=tk.Frame(top,bg=BG)
        groups.pack(side="left",padx=(16,0))
        self.group_buttons={}
        for group,caption in [("d405","D405 × 3"),("d435","D435 × 2"),("webcam","4K RGB × 4")]:
            b=tk.Button(groups,text=caption,command=partial(self.set_group,group),relief="flat",borderwidth=0,
                        font=(FONT,9,"bold"),padx=10,pady=3,cursor="hand2")
            b.pack(side="left",padx=(0,3))
            self.group_buttons[group]=b
        tk.Frame(top,bg=LINE,width=1,height=18).pack(side="left",padx=(10,0))
        switch=tk.Frame(top,bg=BG)
        switch.pack(side="left",padx=(10,0))
        self.layout_buttons={}
        for mode,caption in [("cameras","카메라"),("both","카메라+글러브"),("glove","글러브 3D")]:
            b=tk.Button(switch,text=caption,command=partial(self.set_layout,mode),relief="flat",borderwidth=0,
                        font=(FONT,9,"bold"),padx=10,pady=3,cursor="hand2")
            b.pack(side="left",padx=(0,3))
            self.layout_buttons[mode]=b
        label(top,"더블클릭: 확대 / 복귀   ·   F11: 전체 화면",9,MUTED).pack(side="right")
        self.views=tk.Frame(content,bg=BG)
        self.views.pack(fill="both",expand=True)
        for n in range(2):
            self.views.rowconfigure(n,weight=1,uniform="camera")
        self.panels={}
        for i,key in enumerate(STREAM_NAMES):
            camera_key, kind = key.rsplit("_", 1)
            panel=Preview(self.views,key,partial(self.select_point, camera_key),self.zoom)
            self.panels[key]=panel
        for key,title in EXTRA_TITLES.items():
            self.panels[key]=Preview(self.views,key,lambda *_: None,self.zoom,title)
        self.glove_panel=GlovePanel(self.views)
        self.layout_mode=self.settings.get("monitor_layout","both")
        self.apply_layout()
        self.summary=label(content,"미리보기 최대 15fps · 녹화 목표 30fps · 영상은 이 컴퓨터에만 저장됩니다.",9,MUTED,anchor="w")
        self.summary.pack(fill="x",pady=(10,0))
        self.footer=label(self,"카메라를 검색하고 있습니다…",9,MUTED,anchor="w",wraplength=1500,justify="left")
        self.footer.pack(fill="x",padx=22,pady=12)

    def scrollable(self, parent):
        """Vertical scroll container; the scrollbar appears only when content overflows."""
        canvas = tk.Canvas(parent, bg=PANEL, highlightthickness=0)
        bar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=PANEL)
        window = canvas.create_window(13, 12, window=inner, anchor="nw")
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)

        def refresh(event=None):
            canvas.itemconfigure(window, width=max(1, canvas.winfo_width()-26))
            height = inner.winfo_reqheight() + 24
            canvas.configure(scrollregion=(0, 0, canvas.winfo_width(), height))
            if height > canvas.winfo_height() + 1:
                if not bar.winfo_ismapped():
                    bar.pack(side="right", fill="y", before=canvas)
            elif bar.winfo_ismapped():
                bar.pack_forget()
                canvas.yview_moveto(0)

        def wheel(event):
            if bar.winfo_ismapped():
                canvas.yview_scroll(int(-event.delta/120), "units")

        inner.bind("<Configure>", refresh)
        canvas.bind("<Configure>", refresh)
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        return inner

    def persist(self):
        try:
            self.settings.update(recording_directory=str(self.output_dir),
                                 selected_streams=[k for k,v in self.selections.items() if v.get()],
                                 manus_record=bool(self.record_glove.get()),
                                 vive_record=bool(self.record_tracker.get()))
            write_settings(self.settings)
        except OSError as error:
            logging.warning("Settings save failed: %s",error)

    def update_calibration_metrics(self):
        board = self.calibration["board"] if self.calibration else BOARD_SPEC
        columns, rows = board["squares"]
        self.board_title.configure(text=f"ChArUco {columns} × {rows} · {board['square_m']*1000:.3f} mm")
        self.board_detail.configure(text=f"마커 {board['marker_m']*1000:.3f} mm · DICT_6X6_250 · 큐브 50 mm")
        status = "Depth 기준 추정 크기 적용" if self.calibration and self.calibration.get("scale_optimization") else "보드 기준 외부 보정 적용"
        self.calibration_status.configure(text=(status + " · " + self.calibration["created_at"][:19].replace("T", " "))
                                          if self.calibration else "보정 파일이 없습니다.")
        for key, widget in self.calibration_metrics.items():
            data = self.validation.get("cameras", {}).get(key) if self.validation else None
            if data:
                error = data["reprojection_px"]
                checks = data["depth_plane_mm"]
                checks = checks if isinstance(checks, list) else [checks]
                values = sorted(check["median"] for check in checks if check.get("count"))
                depth = f"{median(values):.1f} mm" if values else "측정 불가"
                pixels = f"{error['rms']:.2f} px" if error.get("count") else "코너 미검출"
                widget.configure(text=f"{CAMERA_NAMES[key]} · 재투영 RMS {pixels}\n깊이 평면 오차 중앙값 {depth}")
            else:
                widget.configure(text=f"{CAMERA_NAMES[key]} · 검증 대기")

    def run_calibration(self):
        if self.saving or self.closing:
            return
        if self.recorder.busy:
            self.footer.configure(text="녹화를 끝낸 뒤 외부 보정을 실행하세요.")
            return
        try:
            spec = board_spec(self.square_var.get())
        except ValueError as error:
            self.footer.configure(text=f"보드 크기를 확인하세요: {error}")
            return
        serials = self.settings.get("realsense_serials") or []
        if len([serial for serial in serials if serial]) != len(CAMERA_KEYS):
            self.footer.configure(text="세 카메라가 모두 배치된 상태에서 외부 보정을 실행하세요.")
            return
        fit_scale_enabled = self.fit_depth_scale.get()
        if not messagebox.askokcancel(
                "외부 보정 실행",
                f"보드: 사각형 {spec['square_m']*1000:g} mm · 마커 {spec['marker_m']*1000:.2f} mm (25 : 18)\n"
                f"Depth 스케일 추가 보정: {'사용' if fit_scale_enabled else '사용 안 함'}\n\n"
                "실시간 연결을 잠시 끊고 카메라별 1280×720 영상 16장을 촬영합니다.\n"
                "보드와 카메라를 움직이지 마세요. 완료되면 calibration.json을 교체하고\n"
                "기존 파일은 결과 폴더에 백업합니다.", parent=self):
            return
        self.settings.pop("calibration_board_mm", None)
        self.settings.update(calibration_square_mm=spec["square_m"]*1000,
                             calibration_fit_depth_scale=fit_scale_enabled)
        self.persist()
        self.saving = True
        self.calibrating = True
        self.footer.configure(text="외부 보정 준비 · 실시간 연결을 종료하고 있습니다…")
        previous = self.calibration
        progress = lambda text: self.ui_events.put(("calibration_progress", text))

        def work():
            try:
                self.hub.stop()
                self.hub.join(12)
                if self.hub.running:
                    raise RuntimeError("실시간 카메라 연결이 종료되지 않았습니다.")
                folder = CALIBRATIONS/datetime.now().strftime("checkerboard_%Y%m%d_%H%M%S")
                capture_calibration_frames(folder/"highres", progress=lambda key, index, count: progress(
                    f"보정 영상 촬영 · {CAMERA_NAMES[key]} {index}/{count} · 보드를 움직이지 마세요"))
                progress("ChArUco 코너 검출 및 카메라 위치 계산 중…")
                result, path = calibrate(folder/"highres", spec)
                if fit_scale_enabled:
                    progress("Depth 기준 보드 스케일 보정 중…")
                    result = fit_scale(result)
                    validation = verify_capture(result["source_folder"], result, folder/"heldout_scaled",
                                                result["validation_indices"])
                    for key, camera in result["cameras"].items():
                        camera["heldout_error_px"] = validation["cameras"][key]["reprojection_px"]
                        camera["heldout_depth_plane_mm"] = validation["cameras"][key]["depth_plane_mm"]
                    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                if previous:
                    (folder/"calibration_previous.json").write_text(
                        json.dumps(previous, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary = ACTIVE_CALIBRATION.with_suffix(".tmp")
                temporary.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
                temporary.replace(ACTIVE_CALIBRATION)
                self.ui_events.put(("calibration_done", (folder, result)))
            except Exception as error:
                logging.exception("Extrinsic calibration failed")
                self.ui_events.put(("calibration_failed", str(error)))
        threading.Thread(target=work, daemon=True, name="Extrinsic-calibration").start()

    def calibration_summary(self, result):
        parts = []
        for key in CAMERA_KEYS:
            camera = result["cameras"][key]
            error = camera.get("heldout_error_px", {})
            checks = [check["median"] for check in camera.get("heldout_depth_plane_mm", []) if check.get("count")]
            pixels = f"{error['rms']:.2f}px" if error.get("count") else "—"
            depth = f"{median(checks):.1f}mm" if checks else "—"
            parts.append(f"{CAMERA_NAMES[key]} {pixels}/{depth}")
        return "검증 재투영 RMS/깊이 평면 오차: " + ", ".join(parts)

    def verify_calibration(self):
        self.capture_calibration_view(False)

    def open_calibration_3d(self):
        self.capture_calibration_view(True)

    def capture_calibration_view(self, open_3d):
        if not self.calibration or self.saving:
            return
        self.saving = True
        self.footer.configure(text="현재 RGB-D 프레임을 저장하고 보정 결과를 검증하고 있습니다…")
        def work():
            try:
                frames = self.hub.frames()
                if not all(frames.get(f"{key}_depth") and frames[f"{key}_depth"].live for key in CAMERA_KEYS):
                    raise RuntimeError("세 카메라의 실시간 RGB-D 연결이 필요합니다.")
                folder = self.hub.save_snapshot()
                validation = verify_snapshot(folder, self.calibration)
                self.ui_events.put(("calibration3d" if open_3d else "calibration_verified", (folder, validation)))
            except Exception as error:
                logging.exception("Calibration verification failed")
                self.ui_events.put(("calibration_error", str(error)))
        threading.Thread(target=work, daemon=True, name="Calibration-verification").start()

    def launch_calibration_3d(self, folder):
        self.launch_tool("robohand.calibration.viewer", str(folder))

    @staticmethod
    def launch_tool(module, *arguments):
        """Run another Robohand tool as its own process (Qt and Tk event loops stay separate)."""
        subprocess.Popen([sys.executable, "-m", module, *arguments], cwd=REPO_ROOT,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def open_calibrations(self):
        CALIBRATIONS.mkdir(parents=True, exist_ok=True)
        os.startfile(str(CALIBRATIONS))

    def apply_range(self,silent=False):
        try:
            near,far=float(self.near_var.get()),float(self.far_var.get())
            self.hub.cameras()[self.selected_camera].set_range(near/1000,far/1000)
            self.settings.setdefault("depth_ranges_mm", {})[self.selected_camera] = [near, far]
            if self.selected_camera == "d405":
                self.settings.update(near_mm=near,far_mm=far)
            if not silent:
                self.persist()
                self.footer.configure(text=f"{CAMERA_NAMES[self.selected_camera]} 깊이 표시 범위 {near:g}–{far:g} mm 적용")
        except ValueError as error:
            if not silent:
                self.footer.configure(text=f"깊이 범위를 확인하세요: {error}")

    def toggle_connection(self):
        if self.recorder.busy or self.closing or self.calibrating:
            return
        if self.hub.running:
            self.hub.stop()
            self.extras.stop_all()
        else:
            self.camera_errors.clear()
            self.extra_errors.clear()
            self.hub.start(self.settings.get("realsense_serials"))
            if self.camera_group != "d405":
                self.extras.activate(self.camera_group)

    def reassign(self):
        if self.calibrating:
            return
        if self.hub.running or self.recorder.busy:
            self.footer.configure(text="먼저 전체 연결을 종료한 뒤 장치 배치를 적용하세요.")
            return
        serials=[self.choice_serials.get(combo.get()) for combo in self.combos]
        actual=[serial for serial in serials if serial]
        if len(set(actual))!=len(actual):
            self.footer.configure(text="각 RGB-D 슬롯에는 서로 다른 카메라를 선택하세요.")
            return
        self.camera_errors.clear()
        self.hub.start(serials)

    def toggle_recording(self):
        if self.closing or self.calibrating:
            return
        if self.recorder.busy:
            self.recorder.stop()
            self.record_button.configure(text="저장 마무리 중…",state="disabled")
            return
        try:
            keys=[k for k,v in self.selections.items() if v.get()]
            self.recorder.start(keys,self.output_dir,sources={
                "manus":self.glove if self.record_glove.get() else None,
                "vive":self.tracker if self.record_tracker.get() else None})
            self.persist()
            self.record_state.configure(text="인코더 준비 중…",fg=MUTED)
            self.record_button.configure(text="준비 중…",state="disabled")
        except Exception as error:
            self.footer.configure(text=str(error))

    def choose_folder(self):
        folder=filedialog.askdirectory(title="동영상 저장 폴더",initialdir=str(self.output_dir if self.output_dir.exists() else RECORDINGS.parent))
        if folder:
            self.output_dir=Path(folder)
            self.folder_label.configure(text=str(self.output_dir))
            self.persist()

    def open_folder(self):
        try:
            self.output_dir.mkdir(parents=True,exist_ok=True)
            os.startfile(self.output_dir)
        except OSError as error:
            self.footer.configure(text=f"폴더 열기 실패: {error}")

    def snapshot(self):
        if self.saving or self.closing:
            return
        self.saving=True
        self.footer.configure(text="스냅샷 저장 중…")
        def work():
            try:
                try:
                    folder=self.hub.save_snapshot()
                except RuntimeError:
                    if not any(frame.live for frame in self.extras.frames().values()):
                        raise
                    folder=None
                folder=self.extras.save_snapshot(folder)  # full-resolution D435 / 4K frames of the shown group
                self.ui_events.put(("snapshot",str(folder)))
            except Exception as error:
                logging.exception("Snapshot failed")
                self.ui_events.put(("snapshot_error",str(error)))
        threading.Thread(target=work,daemon=True).start()

    def open_3d(self):
        self.launch_tool("robohand.replay.player")

    def select_depth_camera(self, event=None):
        name = self.depth_camera.get()
        self.selected_camera = next(key for key, caption in CAMERA_NAMES.items() if caption == name)
        camera = self.hub.cameras()[self.selected_camera]
        self.near_var.set(f"{camera.near*1000:g}")
        self.far_var.set(f"{camera.far*1000:g}")

    def select_point(self,camera_key,x,y):
        self.points[camera_key]=(x,y)
        self.depth_camera.set(CAMERA_NAMES[camera_key])
        self.select_depth_camera()

    def group_layout(self):
        """(panel key -> (row, column), column count) for the camera group on screen."""
        if self.camera_group == "webcam":
            keys = EXTRA_GROUPS["webcam"]["streams"]
            return {key: divmod(index, 2) for index, key in enumerate(keys)}, 2
        slots = CAMERA_KEYS if self.camera_group == "d405" else EXTRA_GROUPS["d435"]["slots"]
        return {f"{slot}_{kind}": (row, column) for column, slot in enumerate(slots)
                for row, kind in enumerate(("color", "depth"))}, len(slots)

    def set_group(self, group):
        if group == self.camera_group:
            return
        self.camera_group = group
        self.focused = None
        self.settings["camera_group"] = group
        self.persist()
        self.extra_errors.clear()
        self.extras.activate(None if group == "d405" else group)
        if group != "d405":
            self.footer.configure(text=f"{EXTRA_GROUPS[group]['title']} 카메라를 여는 중… (미리보기 전용 · 녹화에는 아직 포함되지 않습니다)")
        else:
            self.footer.configure(text="D405 화면 · D435 / 4K RGB 카메라는 정지했습니다.")
        self.apply_layout()

    def zoom(self,key):
        self.focused=None if self.focused==key else key
        self.apply_layout()

    def set_layout(self,mode):
        self.layout_mode=mode
        self.focused=None
        self.settings["monitor_layout"]=mode
        self.persist()
        self.apply_layout()

    def apply_layout(self):
        """Camera grid of the selected group in the first columns; the glove panel takes the next
        column ('both') or everything ('glove')."""
        mode=self.layout_mode if self.layout_mode in ("cameras","both","glove") else "both"
        positions,columns=self.group_layout()
        for n in range(5):
            used=n<columns or (mode=="both" and n==columns)
            self.views.columnconfigure(n,weight=1 if used else 0,uniform="camera" if used else "")
        for panel in [*self.panels.values(),self.glove_panel]:
            panel.grid_remove()
        if mode=="glove":
            self.glove_panel.grid(row=0,column=0,rowspan=2,columnspan=columns+1,sticky="nsew",padx=4,pady=4)
        elif self.focused in positions:
            self.panels[self.focused].grid(row=0,column=0,rowspan=2,columnspan=columns,sticky="nsew",padx=4,pady=4)
        else:
            for key,(row,column) in positions.items():
                self.panels[key].grid(row=row,column=column,rowspan=1,columnspan=1,sticky="nsew",padx=4,pady=4)
        if mode=="both":
            self.glove_panel.grid(row=0,column=columns,rowspan=2,columnspan=1,sticky="nsew",padx=4,pady=4)
        self.glove_panel.last_token=None
        for key,b in [*self.layout_buttons.items(),*self.group_buttons.items()]:
            selected=key in (mode,self.camera_group)
            b.configure(bg=ACCENT if selected else CARD,fg=BG if selected else TEXT,
                        activebackground="#a6f3df" if selected else LINE)

    def escape(self):
        if self.focused:
            self.zoom(self.focused)
        else:
            self.attributes("-fullscreen",False)

    def tick(self):
        while not self.hub.events.empty():
            kind,value=self.hub.events.get()
            if kind=="devices":
                descriptors = list(self.hub.available)
                for device in value:
                    if device and not any(item["serial"] == device["serial"] for item in descriptors):
                        descriptors.append(device)
                self.choice_serials = {f"{device['serial']} · USB {device['usb']}": device["serial"] for device in descriptors}
                values=list(self.choice_serials)
                for i,combo in enumerate(self.combos):
                    combo.configure(values=values)
                    serial=value[i]["serial"] if value[i] else None
                    combo.set(next((name for name, item_serial in self.choice_serials.items() if item_serial==serial),"카메라 없음"))
                self.settings["realsense_serials"]=[device["serial"] if device else None for device in value]
                self.persist()
                self.footer.configure(text="카메라 연결 완료 · 녹화할 영상을 선택한 뒤 녹화 시작을 누르세요.")
            elif kind=="error":
                self.footer.configure(text=value)
        for key,camera in self.hub.cameras().items():
            while not camera.events.empty():
                kind,value=camera.events.get()
                if kind=="error":
                    self.camera_errors[key]=value
                    self.footer.configure(text=f"{key}: {value}")
                elif kind=="info":
                    self.camera_errors.pop(key,None)
        while not self.extras.events.empty():
            event=self.extras.events.get()
            if event[0]=="error":
                self.footer.configure(text=event[1])
            elif event[0]=="devices" and event[1]==self.camera_group:
                title=EXTRA_GROUPS[event[1]]["title"]
                expected=len(EXTRA_GROUPS[event[1]]["slots"])
                self.footer.configure(text=f"{title} {len(event[2])}/{expected}대 검색됨 · 미리보기 전용 (녹화에는 아직 포함되지 않습니다)")
        for slot,kind,value in self.extras.drain_errors():
            if kind=="error":
                self.extra_errors[slot]=value
                self.footer.configure(text=f"{slot}: {value}")
            else:
                self.extra_errors.pop(slot,None)
        while not self.recorder.events.empty():
            kind,value=self.recorder.events.get()
            if kind=="started":
                self.footer.configure(text=f"녹화 중 · {value}")
                self.record_state.configure(text="선택한 영상을 동시 녹화 중",fg=RED)
            elif kind=="finalizing":
                self.record_state.configure(text="MP4 파일을 마무리하고 있습니다",fg=MUTED)
            elif kind=="saved":
                warnings=self.recorder.result.get("warnings",[]) if self.recorder.result else []
                self.footer.configure(text=f"동영상 저장 완료 · {value}" + (" · 수신 누락 있음: session.json 확인" if warnings else ""))
                self.record_state.configure(text="저장 완료 · 수신 누락 있음" if warnings else "저장 완료",fg=AMBER if warnings else ACCENT)
            elif kind=="error":
                self.footer.configure(text=f"녹화 오류: {value}")
                self.record_state.configure(text="오류 · 아래 메시지를 확인하세요",fg=RED)
        while not self.ui_events.empty():
            kind,value=self.ui_events.get()
            if kind == "calibration_progress":
                self.footer.configure(text=value)
                continue
            self.saving=False
            if kind in {"calibration_done", "calibration_failed"}:
                self.calibrating = False
                if kind == "calibration_done":
                    folder, self.calibration = value
                    self.validation = None
                    self.show_calibration.set(True)
                    self.update_calibration_metrics()
                    self.footer.configure(text=f"외부 보정 완료 · {folder}  ·  {self.calibration_summary(self.calibration)}"
                                               "  ·  '프레임 검증'으로 실시간 해상도에서 확인하세요.")
                else:
                    self.footer.configure(text=f"외부 보정 실패 · 기존 보정 유지 · {value}")
                if not self.closing:
                    self.camera_errors.clear()
                    self.hub.start(self.settings.get("realsense_serials"))
            elif kind in {"calibration3d", "calibration_verified"}:
                folder, self.validation = value
                self.update_calibration_metrics()
                self.footer.configure(text=f"Real / Sim 검증 저장 완료 · {folder}")
                if kind == "calibration3d":
                    self.launch_calibration_3d(folder)
            else:
                self.footer.configure(text=f"스냅샷 저장 완료 · {value}" if kind=="snapshot" else f"저장 / 검증 실패 · {value}")
        frames=self.hub.frames()
        live_count=sum(bool(frames.get(f"{key}_color") and frames[f"{key}_color"].live) for key in CAMERA_KEYS)
        busy,recording=self.recorder.busy,self.recorder.recording
        self.top_status.configure(text=f"●  REC · {len(self.recorder.tracks)}개 영상" if recording else f"●  카메라 {live_count}/3 연결",
                                  fg=RED if recording else ACCENT if live_count else MUTED)
        for key,widget in self.device_labels.items():
            frame=frames.get(f"{key}_color")
            state="연결 오류" if key in self.camera_errors else f"LIVE · {frame.fps:.1f} fps" if frame and frame.live else "연결 중" if self.hub.cameras().get(key) and self.hub.cameras()[key].running and not self.hub.cameras()[key].stop_event.is_set() else "정지 / 없음"
            widget.configure(text=state,fg=RED if key in self.camera_errors else ACCENT if frame and frame.live else MUTED)
            camera = self.hub.cameras()[key]
            info = camera.snapshot()[1]
            self.device_details[key].configure(text=f"S/N {camera.serial or '—'} · USB {info.get('usb', '—')}")
        self.update_glove_status(recording)
        self.update_tracker_status(recording)
        self.connect_button.configure(text="전체 연결 종료" if self.hub.running else "전체 카메라 연결",state="disabled" if busy or self.closing or self.hub.scanning else "normal")
        stopping=busy and self.recorder.stop_event.is_set()
        self.record_button.configure(text="저장 마무리 중…" if stopping else "■  녹화 종료 / 저장" if recording else "준비 중…" if busy else "●  녹화 시작   Ctrl+R",
                                     state="disabled" if self.closing or stopping or (busy and not recording) else "normal",
                                     bg=RED if recording else ACCENT)
        for widget in [*self.checks,self.folder_button,self.reassign_button]:
            widget.configure(state="disabled" if busy or self.closing else "normal")
        for combo in self.combos:
            combo.configure(state="disabled" if busy or self.closing else "readonly")
        self.snapshot_button.configure(state="disabled" if self.saving or self.closing else "normal")
        for widget in (self.verify_button, self.overlay3d_button):
            widget.configure(state="disabled" if self.saving or self.closing or not self.calibration else "normal")
        locked = "disabled" if self.saving or self.closing or busy else "normal"
        for widget in (self.calibrate_button, self.fit_depth_check, *self.board_entries):
            widget.configure(state=locked)
        self.calibrate_button.configure(text="외부 보정 진행 중…" if self.calibrating else "외부 보정 실행")
        if self.calibrating:
            self.connect_button.configure(state="disabled")
            self.record_button.configure(state="disabled")
        if self.recorder.started_at and busy:
            total=int(time.monotonic()-self.recorder.started_at)
            self.timer.configure(text=f"{total//3600:02}:{total//60%60:02}:{total%60:02}",fg=RED if recording else MUTED)
        packets = self.hub.packets()
        measurements = {}
        distances = {}
        for camera_key, raw in packets.items():
            mm = distance_mm(raw.depth, raw.scale, *self.points[camera_key]) if raw else None
            distances[camera_key] = mm
            measurements[camera_key] = f"{mm:,.1f} mm" if mm is not None else "측정 불가"
        mm = distances[self.selected_camera]
        self.depth_distance.configure(text=f"{mm:,.1f} mm" if mm is not None else "— mm")
        self.depth_details.configure(text=f"{CAMERA_NAMES[self.selected_camera]} 픽셀 {self.points[self.selected_camera]} · 5×5 중앙값")
        extra_frames = self.extras.frames() if self.camera_group != "d405" else {}
        for key,panel in self.panels.items():
            camera_key = key.rsplit("_", 1)[0]
            if camera_key not in CAMERA_KEYS:
                if self.camera_group != "d405" and key in EXTRA_GROUPS[self.camera_group]["streams"]:
                    panel.render(extra_frames.get(key), None, "", False)
                continue
            overlay = None
            if self.show_calibration.get() and self.calibration and key.endswith("_color"):
                info = self.hub.cameras()[camera_key].snapshot()[1]
                calibrated_camera = self.calibration["cameras"].get(camera_key)
                if calibrated_camera and info.get("serial") == calibrated_camera["serial"]:
                    overlay = (calibrated_camera, info["color_intrinsics"])
            panel.render(frames.get(key),self.points[camera_key],measurements[camera_key],recording and key in self.recorder.tracks, overlay)
        if self.camera_group == "d405":
            self.summary.configure(text="  ·  ".join(f"{CAMERA_NAMES[key]} 선택 깊이 {measurements[key]}" for key in CAMERA_KEYS))
        else:
            self.summary.configure(text=self.extra_summary(extra_frames))
        if self.closing:
            if not busy:
                self.hub.stop()
                if ((not self.hub.running and not self.glove.running and not self.tracker.running
                     and not self.extras.running)
                        or time.monotonic()-self.close_started>12) and not self.saving:
                    self.destroy()
                    return
        self.after(65,self.tick)

    def extra_summary(self, frames):
        parts=[]
        cameras=self.extras.cameras()
        for slot in EXTRA_GROUPS[self.camera_group]["slots"]:
            name=EXTRA_TITLES[f"{slot}_color"].replace(" 컬러","")
            camera=cameras.get(slot)
            frame=frames.get(f"{slot}_color")
            if camera is None:
                parts.append(f"{name} 없음")
            elif slot in self.extra_errors:
                parts.append(f"{name} 오류")
            elif frame and frame.live:
                info=camera.snapshot()[1]
                usb=f" · USB {info['usb']}" if info.get("usb") else ""
                parts.append(f"{name} {frame.fps:.0f} fps{usb}")
            else:
                parts.append(f"{name} 연결 중")
        return "  ·  ".join(parts)+"   |   미리보기 전용"

    def update_glove_status(self, recording):
        status = self.glove.status()
        gloves = sorted(status["gloves"].values(), key=lambda g: g["side"] or "~")
        rates = " · ".join(f"{(g['side'] or '?')[0].upper()} {g['hz']:.0f} Hz" for g in gloves if g["hz"] > 0)
        if status["live"]:
            text = f"{'● REC  ' if recording and 'manus' in self.recorder.sources else ''}LIVE · {rates}"
            color = RED if recording and "manus" in self.recorder.sources else ACCENT
        elif status["connected"]:
            text, color = "데이터 없음", AMBER
        elif self.glove.running:
            text, color = "연결 중", MUTED
        else:
            text, color = "연결 안 됨 · 클릭해 재시도", RED if self.glove.error else MUTED
        detail = (f"Core {status['core_version']}" if status["live"] else status["message"]) or ""
        self.glove_label.configure(text=text, fg=color)
        self.glove_detail.configure(text=detail)
        if self.layout_mode != "cameras":
            self.glove_panel.render(self.glove.hands() if status["live"] else {},
                                    text if status["live"] else (status["message"] or text)[:40], color)

    def retry_glove(self, event=None):
        if not self.glove.running and not self.closing:
            self.glove.start()

    STATE_CAPTIONS = {"tracking": "광학", "inertial": "관성", "stale": "끊김", "calibrating": "보정 중", "waiting": "대기"}

    def update_tracker_status(self, recording):
        status = self.tracker.status()
        recording_tracker = recording and "vive" in self.recorder.sources
        if status["live"]:
            parts = [f"{t['key']} {t['hz']:.0f} Hz · {self.STATE_CAPTIONS.get(t['tracking'], t['tracking'])}"
                     for t in status["trackers"]]
            text = ("● REC  " if recording_tracker else "LIVE · ") + " / ".join(parts)
            color = RED if recording_tracker else ACCENT
            battery = [f"{t['key']} 배터리 {t['battery']}%{' 충전 중' if t.get('charging') else ''}"
                       for t in status["trackers"] if t.get("battery") is not None]
            detail = f"{status['message']}" + (" · " + " ".join(battery) if battery else "")
        elif status["running"]:
            text, color, detail = "연결됨 · 추적 대기" if status["ready"] else "연결 중", AMBER, status["message"]
        else:
            text, color = "연결 안 됨 · 클릭해 연결", RED if self.tracker.error else MUTED
            detail = status["message"] if self.tracker.error else "Tracker Studio에서 페어링/보정 시 연결 해제"
        self.tracker_label.configure(text=text, fg=color)
        self.tracker_detail.configure(text=detail)

    def toggle_tracker(self, event=None):
        if self.closing:
            return
        if self.recorder.busy and "vive" in self.recorder.sources:
            self.footer.configure(text="녹화 중에는 트래커 연결을 바꿀 수 없습니다.")
            return
        if self.tracker.running:
            threading.Thread(target=self.tracker.stop, daemon=True, name="VIVE-stop").start()
            self.footer.configure(text="VIVE 트래커 연결을 해제했습니다 · 이제 Tracker Studio에서 페어링/보정할 수 있습니다.")
        else:
            self.tracker.error = None
            self.tracker.start()

    def close(self):
        if self.closing:
            return
        self.closing=True
        self.close_started=time.monotonic()
        self.persist()
        self.glove.stop_event.set()
        self.extras.stop_all()
        threading.Thread(target=self.tracker.stop, daemon=True, name="VIVE-stop").start()
        if self.recorder.busy:
            self.recorder.stop()
        else:
            self.hub.stop()
        self.footer.configure(text="녹화 파일을 안전하게 저장하고 카메라 연결을 종료합니다…")


def main():
    configure_logging("camera_studio")
    try:
        Viewer().mainloop()
    except Exception:
        logging.exception("Camera Studio failed")
        messagebox.showerror("Camera Studio",f"실행 중 오류가 발생했습니다.\n{LOGS/'camera_studio.log'}")
        raise


if __name__ == "__main__":
    main()
