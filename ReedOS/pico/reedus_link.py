# WiFi link agent for the Pico W. USB is only for the REPL.
# copy to the Pico with reedus_hw.py + reedus_pid.py + wifi_config.py, then
# `import reedus_link` in main.py so it starts on boot.
#
# discovery: UDP beacon on :8267 every 2s so reedosd finds the robot itself.
# safety: no drive command within watchdog_ms, TCP client gone, or WiFi drop
# -> full stop. move/turn maneuvers are exempt because they self-terminate.

import json
import network
import socket
import select
import time
from machine import Pin

from reedus_pid import DriveController, MAX_RPM, TURN_SIGN

try:
    from wifi_config import WIFI_SSID, WIFI_PASSWORD
except ImportError:
    raise SystemExit("Create wifi_config.py with WIFI_SSID and WIFI_PASSWORD")

PORT          = 8266
BEACON_PORT   = 8267
BEACON_EVERY  = 2000      # ms
TELEMETRY_HZ  = 10
FW            = "0.2.0"

led = Pin("LED", Pin.OUT)
drive = DriveController()

cfg = {
    "watchdog_ms": 400,
    "speed_limit": 100,       # percent of MAX_RPM
}

last_cmd_ms = 0
driving = False


def wifi_connect():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        print("joining", WIFI_SSID, "...")
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        t0 = time.ticks_ms()
        while not wlan.isconnected():
            led.toggle()
            time.sleep_ms(250)
            if time.ticks_diff(time.ticks_ms(), t0) > 30000:
                print("wifi timeout, retrying")
                wlan.disconnect()
                wlan.connect(WIFI_SSID, WIFI_PASSWORD)
                t0 = time.ticks_ms()
    led.on()
    ip = wlan.ifconfig()[0]
    print("=" * 52)
    print("Reedus online at %s:%d" % (ip, PORT))
    print("Broadcasting discovery beacon on UDP %d" % BEACON_PORT)
    print("fw %s   TURN_SIGN=%d   MAX_RPM=%.0f" % (FW, TURN_SIGN, MAX_RPM))
    print("=" * 52)
    return wlan, ip


def scale(v):
    return v * (cfg["speed_limit"] / 100.0)


def handle(msg, out):
    global last_cmd_ms, driving
    cmd = msg.get("cmd")

    if cmd == "drive":
        # normalized wheel commands -1..1 -> rpm setpoints
        drive.set_wheels(scale(float(msg.get("l", 0))) * MAX_RPM,
                         scale(float(msg.get("r", 0))) * MAX_RPM)
        last_cmd_ms = time.ticks_ms()
        driving = True

    elif cmd == "twist":
        # joystick / arrow keys: forward + turn, both -1..1
        drive.set_twist(scale(float(msg.get("fwd", 0))),
                        scale(float(msg.get("turn", 0))))
        last_cmd_ms = time.ticks_ms()
        driving = True

    elif cmd == "move":
        drive.move_mm(float(msg.get("mm", 0)))
        driving = False
        out.append({"evt": "maneuver", "kind": "move", "state": "start"})

    elif cmd == "turn":
        drive.turn_deg(float(msg.get("deg", 0)))
        driving = False
        out.append({"evt": "maneuver", "kind": "turn", "state": "start"})

    elif cmd == "stop":
        drive.stop()
        driving = False

    elif cmd == "cfg":
        for k in ("watchdog_ms", "speed_limit"):
            if k in msg:
                cfg[k] = msg[k]

    elif cmd == "ping":
        out.append({"evt": "pong", "t": msg.get("t", 0)})


def bind_listen(srv, attempts=6):
    """Bind with retries."""
    for i in range(attempts):
        try:
            srv.bind(("0.0.0.0", PORT))
            srv.listen(1)
            return True
        except OSError as e:
            print("port %d still busy (%s), retry %d/%d" % (PORT, e, i + 1, attempts))
            time.sleep(1.5)
    return False


def serve():
    global driving

    wlan, ip = wifi_connect()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if not bind_listen(srv):
        srv.close()
        print("")
        print("Could not bind port %d. Hard-reset the board:" % PORT)
        print("    import machine; machine.reset()")
        return
    srv.setblocking(False)

    beacon = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    beacon.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    poller = select.poll()
    poller.register(srv, select.POLLIN)

    client = None
    buf = b""
    last_telem = time.ticks_ms()
    last_beacon = time.ticks_ms()
    was_maneuvering = False

    try:
      while True:
        now = time.ticks_ms()

        # control loop (self-limits to 50 Hz)
        drive.update()

        # discovery beacon
        if time.ticks_diff(now, last_beacon) >= BEACON_EVERY:
            try:
                beacon.sendto(
                    json.dumps({"reedus": True, "ip": ip, "port": PORT, "fw": FW}),
                    ("255.255.255.255", BEACON_PORT))
            except OSError:
                pass
            last_beacon = now

        # accept / read
        for sock, _flag in poller.poll(0):
            if sock is srv:
                try:
                    c, addr = srv.accept()
                    if client:
                        poller.unregister(client)
                        client.close()
                    client = c
                    client.setblocking(False)
                    poller.register(client, select.POLLIN)
                    buf = b""
                    client.send((json.dumps({
                        "evt": "hello", "name": "reedus", "fw": FW,
                        "max_rpm": MAX_RPM, "closed_loop": True}) + "\n").encode())
                    print("reedosd connected from", addr)
                except OSError:
                    pass
            elif client and sock is client:
                try:
                    data = client.recv(512)
                except OSError:
                    data = b""
                if not data:
                    poller.unregister(client)
                    client.close()
                    client = None
                    drive.stop()
                    driving = False
                    print("reedosd gone, motors stopped")
                    continue
                buf += data
                out = []
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        handle(json.loads(line), out)
                    except (ValueError, KeyError):
                        pass
                for o in out:
                    try:
                        client.send((json.dumps(o) + "\n").encode())
                    except OSError:
                        pass

        # watchdog (continuous driving only)
        if driving and time.ticks_diff(now, last_cmd_ms) > cfg["watchdog_ms"]:
            drive.stop()
            driving = False

        # maneuver completion
        if was_maneuvering and drive.maneuver is None and client:
            try:
                client.send((json.dumps({"evt": "maneuver", "state": "done"}) + "\n").encode())
            except OSError:
                pass
        was_maneuvering = drive.maneuver is not None

        # telemetry
        if client and time.ticks_diff(now, last_telem) >= 1000 // TELEMETRY_HZ:
            try:
                client.send((json.dumps(drive.telemetry()) + "\n").encode())
            except OSError:
                pass
            last_telem = now

        # wifi drop safety
        if not wlan.isconnected():
            drive.stop()
            driving = False
            led.off()
            wlan, ip = wifi_connect()

        time.sleep_ms(2)

    finally:
        drive.stop()
        for s in (client, srv, beacon):
            try:
                if s:
                    s.close()
            except Exception:
                pass
        print("sockets closed, safe to re-run")


try:
    serve()
except KeyboardInterrupt:
    print("\nstopped by user")
finally:
    drive.stop()
    led.off()
