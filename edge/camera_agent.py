"""StrayWatch Camera agent: runs at the edge, next to a hotspot camera (deck slides 4, 6, 8).

YOLOv8n + ByteTrack at 1-2 fps. It looks for ONE thing: an animal LYING in a traffic lane and not moving
(likely hurt or hit, and an accident waiting to happen). Healthy animals standing, eating or walking are ignored.
  * lying posture: the box is low and wide (height/width < --lying-ratio) or the animal's height dropped
    well below the tallest it was seen (it went down)
  * not moving: it hardly moved for --still seconds
When both hold, it posts ONE event per animal to the StrayWatch Agent Room (REST) with an annotated snapshot.
The server's Triage agent reads the snapshot and the tested rules decide; a healthy resting animal is only logged.

Install on the edge box (Raspberry Pi 5 class or any PC):  pip install ultralytics lap opencv-python-headless
Run:  python edge/camera_agent.py --video clip.mp4 --camera ratu-rd-01 --server https://<your-app> --key $SW_AGENT_KEY
      (--video also accepts an RTSP URL from the city camera network)
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import sys
import time
import urllib.request
from pathlib import Path

import cv2
import numpy as np
from ultralytics import YOLO

ANIMALS = {19: "cow", 16: "dog", 17: "horse", 18: "sheep"}  # COCO ids; YOLO often calls Indian cattle "horse"/"sheep"
SPECIES = {"cow": "cow", "horse": "cow", "sheep": "cow", "dog": "dog"}


def load_camera(cam_id: str) -> dict:
    data = json.loads((Path(__file__).resolve().parent.parent / "data" / "cameras.json").read_text())
    return data["cameras"][cam_id]


def post_event(server: str, key: str, event: dict) -> dict:
    req = urllib.request.Request(server.rstrip("/") + "/api/agents/camera/events", data=json.dumps(event).encode(),
                                 headers={"Content-Type": "application/json", "X-Agent-Key": key}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def snapshot(frame, poly, boxes, alert_tid, label) -> str:
    vis = frame.copy()
    cv2.polylines(vis, [poly], True, (0, 210, 255), 3)
    for tid, (x1, y1, x2, y2), lab, secs in boxes:
        col = (0, 0, 255) if tid == alert_tid else (80, 200, 60)
        cv2.rectangle(vis, (int(x1), int(y1)), (int(x2), int(y2)), col, 3)
        cv2.putText(vis, f"{lab} {secs:.0f}s", (int(x1), max(22, int(y1) - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.8, col, 2)
    cv2.putText(vis, label, (16, 38), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 3)
    h, w = vis.shape[:2]
    if w > 960:
        vis = cv2.resize(vis, (960, int(h * 960 / w)))
    ok, buf = cv2.imencode(".jpg", vis, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return base64.b64encode(buf.tobytes()).decode()


def run(a) -> dict:
    cam = load_camera(a.camera)
    model = YOLO(a.model)
    cap = cv2.VideoCapture(a.video)
    if not cap.isOpened():
        sys.exit(f"cannot open {a.video}")
    fps = cap.get(cv2.CAP_PROP_FPS)
    fps = fps if 1 <= fps <= 120 else 30.0  # some web videos report nonsense fps
    step = max(1, round(fps / a.fps))
    W, H = int(cap.get(3)), int(cap.get(4))
    poly = (np.array(cam["lane"]) * [W, H]).astype(np.int32)
    diag = math.hypot(W, H)

    first_in, history, sent, events, ms, tallest = {}, {}, set(), [], [], {}
    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        t = idx / fps
        if t > a.max_seconds:
            break
        if idx % step == 0:
            t0 = time.perf_counter()
            r = model.track(frame, persist=True, tracker="bytetrack.yaml", classes=list(ANIMALS), conf=a.conf,
                            imgsz=640, verbose=False)[0]
            ms.append((time.perf_counter() - t0) * 1000)
            boxes = []
            if r.boxes.id is not None:
                for box, tid, cls in zip(r.boxes.xyxy.tolist(), r.boxes.id.int().tolist(), r.boxes.cls.int().tolist()):
                    x1, y1, x2, y2 = box
                    foot = ((x1 + x2) / 2, y2)
                    if cv2.pointPolygonTest(poly, foot, False) < 0:
                        first_in.pop(tid, None)
                        history.pop(tid, None)
                        continue
                    first_in.setdefault(tid, t)
                    hist = history.setdefault(tid, [])
                    hist.append((t, (x1 + x2) / 2, (y1 + y2) / 2))
                    lab = SPECIES[ANIMALS[cls]]
                    boxes.append((tid, box, lab, t - first_in[tid]))
            for tid, box, lab, secs in boxes:
                if tid in sent:
                    continue
                x1, y1, x2, y2 = box
                bw, bh = max(1.0, x2 - x1), max(1.0, y2 - y1)
                tallest[tid] = max(tallest.get(tid, 0.0), bh)
                recent = [p for p in history[tid] if p[0] >= t - a.still]
                moved = max((math.hypot(p[1] - recent[0][1], p[2] - recent[0][2]) for p in recent), default=0) / diag
                lying = bh / bw < a.lying_ratio or bh < 0.7 * tallest[tid]
                still_long = t - history[tid][0][0] >= a.still and moved < 0.03
                if not (lying and still_long):
                    continue
                kind = "lying"
                sent.add(tid)
                label = f"t={t:.1f}s ALERT: {lab} lying in lane {secs:.0f}s"
                ev = {"camera_id": a.camera, "kind": kind, "species": lab, "seconds_in_lane": round(secs, 1),
                      "track_id": tid, "video_time_s": round(t, 1), "animals_in_lane": len(boxes),
                      "snapshot_jpeg_b64": snapshot(frame, poly, boxes, tid, label)}
                print(f"[{t:6.1f}s] LYING: {lab} #{tid} in lane {secs:.0f}s", flush=True)
                if a.save_events:
                    Path(a.save_events).mkdir(parents=True, exist_ok=True)
                    (Path(a.save_events) / f"event_{len(events) + 1}.json").write_text(json.dumps(ev))
                if a.server:
                    try:
                        res = post_event(a.server, a.key, ev)
                        print("   → Agent Room:", res.get("summary", res), flush=True)
                        ev["result"] = res
                    except Exception as e:  # noqa: BLE001 - keep watching even if the network blips
                        print("   → could not reach the Agent Room:", e, flush=True)
                events.append(ev)
                if len(events) >= a.max_events:
                    break
            if len(events) >= a.max_events:
                break
        idx += 1
    out = {"camera": a.camera, "seconds": round(idx / fps, 1), "frames_analysed": len(ms),
           "ms_per_frame_median": round(float(np.median(ms)), 1) if ms else None,
           "events": [{k: v for k, v in e.items() if k != "snapshot_jpeg_b64"} for e in events]}
    print(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--video", required=True, help="file path or RTSP URL")
    p.add_argument("--camera", required=True, help="camera id from data/cameras.json")
    p.add_argument("--server", default="", help="StrayWatch base URL; empty = dry run")
    p.add_argument("--key", default="", help="agent key (SW_AGENT_KEY on the server)")
    p.add_argument("--model", default="yolov8n.pt")
    p.add_argument("--fps", type=float, default=2.0, help="frames analysed per second (deck: 1-2 fps)")
    p.add_argument("--still", type=float, default=30.0, help="seconds lying without moving before an alert")
    p.add_argument("--lying-ratio", type=float, default=0.62, help="box height/width below this = lying posture")
    p.add_argument("--conf", type=float, default=0.25)
    p.add_argument("--max-seconds", type=float, default=1e9)
    p.add_argument("--max-events", type=int, default=5)
    p.add_argument("--save-events", default="", help="folder to also save each event as JSON")
    run(p.parse_args())
