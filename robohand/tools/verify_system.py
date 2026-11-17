"""Hardware end-to-end check: all cameras -> H.264 MP4 + FFV1 depth -> full decode -> point cloud.

    python -m robohand.tools.verify_system --seconds 5
"""
import argparse
import json
import time

import cv2

from ..cameras.hub import STREAM_NAMES, CameraHub
from ..cameras.recorder import Recorder
from ..paths import RECORDINGS
from ..replay.reconstruction import DepthSequence


def verify(seconds=5):
    hub=CameraHub()
    hub.start()
    recorder=Recorder(hub)
    try:
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            frames=hub.frames()
            if len(frames)==len(STREAM_NAMES) and all(f.live and f.number>20 for f in frames.values()):
                break
            time.sleep(.1)
        else:
            raise RuntimeError(f"Expected {len(STREAM_NAMES)} live streams, got {list(hub.frames())}")
        print(json.dumps({"devices":hub.metadata(),"live_streams":{k:{"resolution":list(f.image.shape[:2][::-1]),
                         "fps":round(f.fps,2)} for k,f in hub.frames().items()}},ensure_ascii=False,indent=2),flush=True)
        reports=[]
        for duration in [seconds,2]:
            recorder.start(list(STREAM_NAMES),RECORDINGS/"verification")
            deadline=time.monotonic()+15
            while not recorder.recording and recorder.busy and time.monotonic()<deadline:
                time.sleep(.05)
            if not recorder.recording:
                raise RuntimeError(recorder.error or "Recorder did not start")
            time.sleep(duration)
            recorder.stop()
            recorder.thread.join(timeout=45)
            if recorder.busy or recorder.error:
                raise RuntimeError(recorder.error or "Recorder finalization timed out")
            session=json.loads((recorder.folder/"session.json").read_text(encoding="utf-8"))
            assert session["status"]=="complete",session
            report={"folder":str(recorder.folder),"duration_s":session["duration_s"],"tracks":{}}
            for key,track in session["tracks"].items():
                cap=cv2.VideoCapture(str(recorder.folder/track["file"]))
                assert cap.isOpened(),key
                count=0
                shape=None
                while True:
                    ok,image=cap.read()
                    if not ok:break
                    shape=[image.shape[1],image.shape[0]]
                    count+=1
                cap.release()
                assert count==track["frames"] and count>30,(key,count,track)
                assert shape==track['resolution'],(key,shape)
                assert abs(count/30-duration)<.2,(key,count,duration)
                assert track["unavailable_frames"]==0,(key,track)
                report["tracks"][key]={"decoded_frames":count,"resolution":shape,
                                       "repeated_frames":track["repeated_frames"],"bytes":track["bytes"]}
                if key.endswith("_depth"):
                    assert track.get("raw_depth_file"), key
                    raw_cap = cv2.VideoCapture(str(recorder.folder/track["raw_depth_file"]),
                                               cv2.CAP_FFMPEG, [cv2.CAP_PROP_CONVERT_RGB,0])
                    raw_count = 0
                    while True:
                        ok, depth = raw_cap.read()
                        if not ok:
                            break
                        assert str(depth.dtype) == "uint16" and list(depth.shape[::-1]) == shape
                        raw_count += 1
                    raw_cap.release()
                    assert raw_count == count, (key, raw_count, count)
                    camera_key = key.rsplit("_",1)[0]
                    sequence = DepthSequence(recorder.folder, camera_key)
                    assert not sequence.approximate
                    cloud = sequence.load(count//2, far=2.0)
                    assert len(cloud.points) > 100, (camera_key, len(cloud.points))
                    sequence.close()
                    report["tracks"][key].update(raw_depth_frames=raw_count, points_3d=len(cloud.points))
            assert len({t["frames"] for t in session["tracks"].values()})==1
            reports.append(report)
            print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
        snapshot=hub.save_snapshot()
        print(json.dumps({"result":"PASS","recording_sessions":len(reports),"snapshot":str(snapshot)},ensure_ascii=False),flush=True)
    finally:
        if recorder.busy:
            recorder.stop()
            recorder.thread.join(timeout=45)
        hub.stop()
        hub.join()
        assert not hub.running,"Camera readers did not stop"


def main():
    parser=argparse.ArgumentParser(description="End-to-end camera recording check")
    parser.add_argument("--seconds",type=float,default=5)
    verify(parser.parse_args().seconds)


if __name__=="__main__":
    main()
