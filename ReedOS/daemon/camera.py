"""camera, MJPEG streamer for /camera/stream."""

import logging
import threading
import time

log = logging.getLogger("camera")

try:
    import cv2
except ImportError:
    cv2 = None


class Camera:
    def __init__(self, device=0, fps=15, width=1280, height=720, idle_retry_s=120):
        self.device = device
        self.idle_retry_s = idle_retry_s
        self.fps = fps
        self.width = width
        self.height = height
        self.available = False
        self._frame = None
        self._lock = threading.Lock()
        self._thread = None

    def start(self):
        if cv2 is None:
            log.info("opencv not installed, camera disabled (pip install opencv-python-headless)")
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        # no camera is the normal state right now. retrying every 5s buried the log
        # (opencv warns per attempt), so complain twice then check every couple minutes.
        misses = 0
        while True:
            cap = cv2.VideoCapture(self.device)
            if not cap.isOpened():
                self.available = False
                misses += 1
                if misses <= 2:
                    log.info("no camera at %s, will keep an eye out", self.device)
                elif misses == 3:
                    log.info("still no camera at %s, checking every %ds from here, quietly",
                             self.device, self.idle_retry_s)
                time.sleep(5 if misses <= 2 else self.idle_retry_s)
                continue
            misses = 0
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            self.available = True
            log.info("camera %s online", self.device)
            interval = 1.0 / self.fps
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if ok:
                    with self._lock:
                        self._frame = jpg.tobytes()
                time.sleep(interval)
            cap.release()
            self.available = False
            log.warning("camera lost, retrying in 5 s")
            time.sleep(5)

    def mjpeg(self):
        boundary = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
        while True:
            with self._lock:
                frame = self._frame
            if frame:
                yield boundary + frame + b"\r\n"
            time.sleep(1.0 / self.fps)
