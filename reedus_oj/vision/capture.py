#!/usr/bin/env python3
"""camera capture: usb webcam or a phone IP-camera app."""

from __future__ import annotations

import argparse
import sys
import time
from typing import Callable

import numpy as np


def _cv2():
    import cv2
    return cv2


_CANDIDATE_PATHS = ["/live", "/video", "/videofeed", "/shot.jpg", "/live.jpg", ""]


def _candidate_urls(url: str):
    """Yield full URLs to try. A url with a path is used as-is, otherwise common IP-cam paths."""
    if "://" not in url:
        url = "http://" + url
    # split off any path
    scheme_host = url.split("://", 1)
    scheme, rest = scheme_host[0], scheme_host[1]
    host, _, path = rest.partition("/")
    if path:                       # caller gave an explicit path -> respect it
        yield f"{scheme}://{host}/{path}"
        return
    for suffix in _CANDIDATE_PATHS:
        yield f"{scheme}://{host}{suffix}"


def _try_open(cv2, full_url: str, warmup: int, retries: int):
    cap = cv2.VideoCapture(full_url)
    if not cap.isOpened():
        cap.release()
        return None
    try:
        for _ in range(warmup):
            cap.read()
        for _ in range(retries):
            ok, frame = cap.read()
            if ok and frame is not None:
                return frame
            time.sleep(0.2)
    finally:
        cap.release()
    return None


def phone_snapshot(url: str, retries: int = 3, warmup: int = 2) -> np.ndarray:
    """Grab one frame from a phone IP-camera URL."""
    cv2 = _cv2()
    tried = []
    for full_url in _candidate_urls(url):
        tried.append(full_url)
        frame = _try_open(cv2, full_url, warmup, retries)
        if frame is not None:
            if len(tried) > 1:
                print(f"[capture] using {full_url}")
            return frame
    raise RuntimeError(
        "could not grab a frame. Tried:\n  " + "\n  ".join(tried) +
        "\nChecks: phone + PC on same wifi? 'Turn on IP Camera Server' pressed? "
        "Can you open the URL in a browser and see live video? "
        "If it needs a login, put it in the URL: http://admin:admin@host:port"
    )


def resolve_stream(url: str, warmup: int = 2, retries: int = 3):
    """Find the working endpoint for `url` and return an open cv2 capture + the url that worked."""
    cv2 = _cv2()
    tried = []
    for full_url in _candidate_urls(url):
        tried.append(full_url)
        cap = cv2.VideoCapture(full_url)
        if cap.isOpened():
            for _ in range(warmup):
                cap.read()
            ok, frame = cap.read()
            if ok and frame is not None:
                return cap, full_url
        cap.release()
    raise RuntimeError(
        "could not open a stream. Tried:\n  " + "\n  ".join(tried) +
        "\nOpen the URL in a browser first; if it wants a login use "
        "http://admin:admin@host:port"
    )


def grab_fresh(cap, flush: int = 3) -> np.ndarray:
    """Read a current frame from an already-open stream (flush stale buffer)."""
    frame = None
    for _ in range(flush):
        ok, f = cap.read()
        if ok and f is not None:
            frame = f
    if frame is None:
        raise RuntimeError("stream stopped returning frames (phone app closed?)")
    return frame


def interactive(url: str = None, webcam: int = None, show: bool = False):
    """Launch once, then type requests in a loop. Blank line or 'q' to quit."""
    from vision import ReedusVision
    cv2 = _cv2()
    if webcam is not None:
        cap = cv2.VideoCapture(webcam)
        if not cap.isOpened():
            raise RuntimeError(f"could not open webcam index {webcam}. "
                               f"Is the phone-webcam PC client running/connected?")
        cap.read()  # warm up
        source_label = f"webcam {webcam}"
    else:
        cap, source_label = resolve_stream(url)
    print(f"[capture] live on {source_label}")
    vision = ReedusVision()
    print("Type a request and press Enter. Blank line or 'q' quits.\n"
          "e.g.  grab me a coke   |   where are my keys   |   what's on the table")
    try:
        while True:
            try:
                req = input("\nlook> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not req or req.lower() in {"q", "quit", "exit"}:
                break
            try:
                frame = grab_fresh(cap)
            except Exception as exc:
                print(f"  [capture error] {exc}")
                break
            result = vision.look(req, image=frame, debug=True)
            print("  SCENE:", result.text)
            if show:
                disp = frame.copy()
                for d in result.detections:
                    x1, y1, x2, y2 = (int(v) for v in d.box_xyxy)
                    cv2.rectangle(disp, (x1, y1), (x2, y2), (0, 255, 0), 2)
                    cv2.putText(disp, f"{d.label} {d.confidence:.2f}",
                                (x1, max(0, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX,
                                0.5, (0, 255, 0), 1)
                cv2.imshow("Reedus Vision", disp)
                cv2.waitKey(1)   # refresh window without blocking the prompt
    finally:
        cap.release()
        try:
            cv2.destroyAllWindows()
        except Exception:
            pass
        print("bye.")


def webcam_snapshot(index: int = 0, warmup: int = 3) -> np.ndarray:
    """Grab one frame from a local USB/built-in webcam."""
    cv2 = _cv2()
    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        raise RuntimeError(f"could not open webcam index {index}")
    try:
        for _ in range(warmup):
            cap.read()
        ok, frame = cap.read()
        if not ok or frame is None:
            raise RuntimeError("webcam opened but returned no frame")
        return frame
    finally:
        cap.release()


def make_phone_capture(url: str) -> Callable[[], np.ndarray]:
    """Return a zero-arg capture() suitable for ReedusVision(capture=...)."""
    return lambda: phone_snapshot(url)


def make_webcam_capture(index: int = 0) -> Callable[[], np.ndarray]:
    return lambda: webcam_snapshot(index)


def list_cameras(max_index: int = 10):
    """Probe webcam indices and report which ones actually deliver a frame."""
    import os
    os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")
    cv2 = _cv2()
    try:
        cv2.setLogLevel(0)      # silence probe noise for absent devices
    except Exception:
        pass
    backend = getattr(cv2, "CAP_V4L2", 0) if sys.platform.startswith("linux") else 0
    found = []
    print("probing video devices...")
    for i in range(max_index):
        cap = cv2.VideoCapture(i, backend) if backend else cv2.VideoCapture(i)
        if cap.isOpened():
            ok, frame = cap.read()
            if ok and frame is not None:
                h, w = frame.shape[:2]
                print(f"  [{i}] OK  {w}x{h}")
                found.append(i)
            else:
                print(f"  [{i}] opened but no frame")
        cap.release()
    if not found:
        print("no working cameras found. On Linux check: ls /dev/video*  and make "
              "sure OBS 'Start Virtual Camera' is running (needs v4l2loopback).")
    else:
        print(f"\nusable indices: {found}  ->  run with --webcam {found[-1]}")
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description="Test camera capture + a detection look")
    if argv is None:
        argv = sys.argv[1:]
    if "--list-cams" in argv:
        list_cameras()
        return 0

    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="phone IP-camera URL (e.g. http://IP:8080/video)")
    src.add_argument("--webcam", type=int, help="local webcam index, e.g. 0")
    ap.add_argument("--request", default=None,
                    help="a single 'look' request. Omit to type requests interactively.")
    ap.add_argument("--save", default=None, help="save the captured frame to this path")
    ap.add_argument("--show", action="store_true", help="draw boxes and preview")
    ap.add_argument("--list-cams", action="store_true",
                    help="probe video devices and exit (find the right index)")
    args = ap.parse_args(argv)

    if args.request is None:
        interactive(url=args.url, webcam=args.webcam, show=args.show)
        return 0

    frame = phone_snapshot(args.url) if args.url else webcam_snapshot(args.webcam)
    print(f"captured frame {frame.shape[1]}x{frame.shape[0]}")

    if args.save:
        _cv2().imwrite(args.save, frame)
        print(f"saved -> {args.save}")

    # run the full pipeline on the captured frame
    from vision import ReedusVision
    vision = ReedusVision()
    result = vision.look(args.request, image=frame, debug=True)
    print("\nSCENE:", result.text)
    print("TIMINGS(ms):", result.timings_ms)

    if args.show:
        cv2 = _cv2()
        for d in result.detections:
            x1, y1, x2, y2 = (int(v) for v in d.box_xyxy)
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
            cv2.putText(frame, f"{d.label} {d.confidence:.2f}", (x1, max(0, y1 - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        cv2.imshow("Reedus Vision", frame)
        print("\npress any key in the window to close")
        cv2.waitKey(0)
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    sys.exit(main())
