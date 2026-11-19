"""Tk widgets of Camera Studio: styled label/button, camera preview and live glove skeleton."""
from __future__ import annotations

import math
import tkinter as tk

import cv2
from PIL import Image, ImageTk

from ..calibration.board import draw_overlay
from ..cameras.hub import STREAM_NAMES
from ..devices.manus import FINGER_COLORS
from .theme import ACCENT, AMBER, BG, CARD, FONT, LINE, MUTED, PANEL, RED, TEXT


def label(parent, text, size=10, color=TEXT, bold=False, **kw):
    return tk.Label(parent, text=text, bg=parent["bg"], fg=color,
                    font=(FONT, size, "bold" if bold else "normal"), **kw)


def button(parent, text, command, primary=False, **kw):
    return tk.Button(parent, text=text, command=command, bg=ACCENT if primary else CARD,
                     fg=BG if primary else TEXT, activebackground="#a6f3df" if primary else LINE,
                     activeforeground=BG if primary else TEXT, font=(FONT, 10, "bold"),
                     relief="flat", borderwidth=0, padx=12, pady=9, cursor="hand2",
                     disabledforeground="#64778a", **kw)


class Preview(tk.Frame):
    def __init__(self, parent, key, select, zoom, title=None):
        super().__init__(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        self.key, self.select = key, select
        self.photo, self.bounds, self.last_token = None, None, None
        heading = tk.Frame(self, bg=PANEL)
        heading.pack(fill="x", padx=13, pady=(10, 7))
        label(heading, title or STREAM_NAMES[key], 11, bold=True).pack(side="left")
        self.status = label(heading, "대기 중", 8, MUTED)
        self.status.pack(side="right")
        self.canvas = tk.Canvas(self, bg="#080e15", highlightthickness=0,
                                cursor="crosshair" if key.startswith("d405") else "arrow")
        self.canvas.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.image_id = self.canvas.create_image(0, 0, anchor="nw")
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Double-Button-1>", lambda e: zoom(key))
        self.canvas.bind("<Configure>", lambda e: setattr(self, "last_token", None))

    def _click(self, event):
        if not self.key.startswith("d405") or not self.bounds:
            return
        x0,y0,w,h,iw,ih = self.bounds
        if x0 <= event.x < x0+w and y0 <= event.y < y0+h:
            self.select(min(iw-1, int((event.x-x0)*iw/w)), min(ih-1,int((event.y-y0)*ih/h)))

    def render(self, frame, point, measurement, recording, overlay=None):
        if not self.winfo_ismapped():
            return
        c = self.canvas
        cw,ch = c.winfo_width(),c.winfo_height()
        if cw<10 or ch<10:
            return
        live = frame is not None and frame.live
        if frame:
            ih,iw = frame.image.shape[:2]
            sw,sh = frame.source_size or (iw,ih)
            self.status.configure(text=f"{'● REC  ' if recording else ''}{sw} × {sh}  ·  {frame.fps:.1f} fps" if live else "연결 종료 · 마지막 프레임",
                                  fg=RED if recording else ACCENT if live else MUTED)
            token = (frame.number, frame.received_at, point, live, recording, cw,ch, bool(overlay))
        else:
            self.status.configure(text="연결 대기", fg=MUTED)
            token = (None, cw,ch)
        if token == self.last_token:
            return
        self.last_token = token
        c.delete("overlay")
        if frame is None:
            c.itemconfigure(self.image_id,image="")
            c.create_text(cw//2,ch//2, text="카메라 연결을 기다리고 있습니다", fill=MUTED,
                          font=(FONT,10), tags="overlay")
            return
        scale = min(cw/iw, ch/ih)
        w,h = max(1,int(iw*scale)),max(1,int(ih*scale))
        x0,y0 = (cw-w)//2,(ch-h)//2
        self.bounds = (x0,y0,w,h,iw,ih)
        source = frame.image
        is_bgr = frame.bgr
        if overlay:
            source = source if is_bgr else cv2.cvtColor(source, cv2.COLOR_RGB2BGR)
            source = draw_overlay(source, *overlay)
            is_bgr = True
        small = cv2.resize(source,(w,h),interpolation=cv2.INTER_AREA)
        if is_bgr:
            small = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
        self.photo = ImageTk.PhotoImage(Image.fromarray(small))
        c.coords(self.image_id,x0,y0)
        c.itemconfigure(self.image_id,image=self.photo)
        if self.key.startswith("d405"):
            x,y = x0+point[0]*w/iw,y0+point[1]*h/ih
            c.create_line(x-12,y,x+12,y,fill=ACCENT,width=1,tags="overlay")
            c.create_line(x,y-12,x,y+12,fill=ACCENT,width=1,tags="overlay")
            c.create_oval(x-3,y-3,x+3,y+3,outline=ACCENT,width=2,tags="overlay")
            c.create_rectangle(x0+8,y0+8,x0+140,y0+32,fill=PANEL,outline="",tags="overlay")
            c.create_text(x0+17,y0+20,text=measurement,fill=TEXT,font=(FONT,9),anchor="w",tags="overlay")
        if not live:
            c.create_rectangle(0,ch-29,cw,ch,fill=PANEL,outline="",tags="overlay")
            c.create_text(cw//2,ch-15,text="정지된 영상",fill=AMBER,font=(FONT,9),tags="overlay")


class GlovePanel(tk.Frame):
    """Orbitable 3D skeletons of both MANUS gloves, each around its own wrist (no tracker yet)."""
    SIDE_CAPTIONS = {"left": "왼손", "right": "오른손"}
    SPAN_M = .24

    def __init__(self, parent):
        super().__init__(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        heading = tk.Frame(self, bg=PANEL)
        heading.pack(fill="x", padx=13, pady=(10,7))
        label(heading, "MANUS 글러브 3D", 11, bold=True).pack(side="left")
        self.status = label(heading, "대기 중", 8, MUTED)
        self.status.pack(side="right")
        self.canvas = tk.Canvas(self, bg="#080e15", highlightthickness=0, cursor="fleur")
        self.canvas.pack(fill="both", expand=True, padx=8, pady=(0,8))
        self.reset_view()
        self.drag, self.last_token = None, None
        self.canvas.bind("<ButtonPress-1>", lambda e: setattr(self, "drag", (e.x, e.y, self.yaw, self.pitch)))
        self.canvas.bind("<B1-Motion>", self._rotate)
        self.canvas.bind("<Double-Button-1>", lambda e: self.reset_view())
        self.canvas.bind("<Configure>", lambda e: setattr(self, "last_token", None))

    def reset_view(self):
        self.yaw, self.pitch = math.radians(-35), math.radians(25)
        self.last_token = None

    def _rotate(self, event):
        if self.drag:
            x, y, yaw, pitch = self.drag
            self.yaw = yaw + (event.x-x)*.01
            self.pitch = max(-1.5, min(1.5, pitch + (event.y-y)*.01))
            self.last_token = None

    def _project(self, points, cx, cy, scale):
        cos_yaw, sin_yaw = math.cos(self.yaw), math.sin(self.yaw)
        cos_pitch, sin_pitch = math.cos(self.pitch), math.sin(self.pitch)
        result = []
        for x, y, z in points:
            x1, y1 = x*cos_yaw - y*sin_yaw, x*sin_yaw + y*cos_yaw
            depth, up = y1*cos_pitch - z*sin_pitch, y1*sin_pitch + z*cos_pitch
            result.append((cx + x1*scale, cy - up*scale, depth))
        return result

    def render(self, hands, status_text, status_color):
        if not self.winfo_ismapped():
            return
        self.status.configure(text=status_text, fg=status_color)
        c = self.canvas
        width, height = c.winfo_width(), c.winfo_height()
        if width < 20 or height < 20:
            return
        token = (tuple((side, tuple(map(tuple, hand["points"]))) for side, hand in sorted(hands.items())),
                 self.yaw, self.pitch, width, height)
        if token == self.last_token:
            return
        self.last_token = token
        c.delete("all")
        sides = ["left", "right"] + sorted(side for side in hands if side not in ("left", "right"))
        stacked = height > width  # narrow side panel: hands above each other
        slot_w, slot_h = (width, height/len(sides)) if stacked else (width/len(sides), height)
        scale = min(slot_w, slot_h*.85) * .82 / self.SPAN_M
        for index, side in enumerate(sides):
            cx, cy = (width*.5, slot_h*(index+.5)) if stacked else (slot_w*(index+.5), height*.5)
            top = slot_h*index if stacked else 0
            caption = self.SIDE_CAPTIONS.get(side, side)
            hand = hands.get(side)
            if hand is None:
                c.create_text(cx, cy, text=f"{caption}\n데이터 없음", fill=MUTED, font=(FONT, 10), justify="center")
                continue
            points = hand["points"]
            center = [sum(p[k] for p in points)/len(points) for k in range(3)]
            projected = self._project([[p[k]-center[k] for k in range(3)] for p in points], cx, cy, scale)
            segments = [(i, parent) for i, parent in enumerate(hand["parents"]) if i != parent]
            segments.sort(key=lambda s: -(projected[s[0]][2] + projected[s[1]][2]))
            for child, parent in segments:
                color = FINGER_COLORS.get(hand["chains"][child], TEXT)
                c.create_line(projected[parent][0], projected[parent][1], projected[child][0], projected[child][1],
                              fill=color, width=5, capstyle="round")
            for i, (x, y, _) in enumerate(projected):
                radius = 6 if i == 0 else 3
                c.create_oval(x-radius, y-radius, x+radius, y+radius, fill="#edf5fc" if i == 0 else "#080e15",
                              outline=FINGER_COLORS.get(hand["chains"][i], TEXT), width=2)
            c.create_text(cx, top+18, text=caption, fill=TEXT, font=(FONT, 11, "bold"))
        # Small axis gizmo for the MANUS world frame (X red, Y green, Z blue).
        origin = (36, height-34)
        for axis, color in [((1,0,0), "#ff6b6b"), ((0,1,0), "#6ee7a0"), ((0,0,1), "#6ca8ff")]:
            (x, y, _), = self._project([axis], *origin, 22)
            c.create_line(*origin, x, y, fill=color, width=2, arrow="last")
        c.create_text(width-10, height-12, anchor="e", fill=MUTED, font=(FONT, 8),
                      text="손목 기준 · 트래커 미연동" if stacked else
                      "손목 기준 · 트래커 미연동  ·  드래그 회전 · 더블클릭 초기화")
