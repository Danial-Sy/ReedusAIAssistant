# closed-loop drive controller, shared by pid_drive.py and reedus_link.py
#
# gains were measured on this robot by test_pid_characterize.py:
#   Kv = 104 rpm per unit duty (fit over 35-80%, 100% saturates)
#   b  = -9.8 rpm -> zero crossing at 0.095 duty = feedforward offset
#   tau = 0.081 s
# verified: 2.3 rpm mean velocity error, <0.7 deg heading drift over 400mm
#
# set_twist(fwd, turn) for joystick driving, move_mm / turn_deg for closed-loop
# maneuvers that finish themselves. update() self-limits to CTRL_HZ.

from reedus_hw import make_drive, counts_to_rpm, COUNTS_PER_REV
import time

# --- plant model (measured) ---
KV            = 104.0
FF_OFFSET     = 0.095
MAX_DUTY      = 0.85

# --- velocity loop ---
KP            = 0.005
KI            = 0.060
I_CLAMP       = 0.30

# --- heading loop (straight-line only) ---
KP_HEADING    = 0.0002
KI_HEADING    = 0.00015
HEAD_I_CLAMP  = 0.08
HEAD_CLAMP    = 0.15

TURN_SIGN     = -1

# --- motion limits ---
CTRL_HZ       = 50
DT            = 1.0 / CTRL_HZ
MAX_RPM       = 60.0      # what a full-scale command (1.0) means
RAMP_RPM_S    = 140.0     # setpoint slew rate, kills the start lurch

# --- geometry ---
WHEEL_DIA_MM  = 65.0
TRACK_MM      = 150.0     # wheel centre-to-centre

MM_PER_COUNT  = (3.14159 * WHEEL_DIA_MM) / COUNTS_PER_REV
# counts one wheel must turn for the robot to rotate 1 degree on the spot
COUNTS_PER_DEG = (3.14159 * TRACK_MM / 360.0) / MM_PER_COUNT

# maneuver tuning
MANEUVER_RPM      = 35.0
MANEUVER_MIN_RPM  = 12.0     # floor so it doesn't stall before arriving
DECEL_COUNTS      = 2200     # start easing off this far out (~0.5 rev)
ARRIVE_COUNTS     = 90       # close enough
ARRIVE_HOLD_MS    = 150      # must stay inside the window this long


class _PI:
    """Per-wheel velocity PI with feedforward and conditional anti-windup."""

    def __init__(self):
        self.i = 0.0

    def reset(self):
        self.i = 0.0

    def step(self, target_rpm, actual_rpm, dt):
        err = target_rpm - actual_rpm

        ff = 0.0
        if target_rpm > 0.1:
            ff = FF_OFFSET + target_rpm / KV
        elif target_rpm < -0.1:
            ff = -FF_OFFSET + target_rpm / KV

        out = ff + KP * err + self.i
        if abs(out) < MAX_DUTY:               # only integrate when not saturated
            self.i += KI * err * dt
            if self.i > I_CLAMP:
                self.i = I_CLAMP
            elif self.i < -I_CLAMP:
                self.i = -I_CLAMP

        out = ff + KP * err + self.i
        if out > MAX_DUTY:
            out = MAX_DUTY
        elif out < -MAX_DUTY:
            out = -MAX_DUTY
        return out


class DriveController:
    def __init__(self):
        self.left, self.right, self.enc_l, self.enc_r = make_drive()
        self.pi_l = _PI()
        self.pi_r = _PI()

        self.tgt_l = 0.0          # commanded wheel setpoints (rpm)
        self.tgt_r = 0.0
        self.cmd_l = 0.0          # ramped setpoints actually fed to the loops
        self.cmd_r = 0.0
        self.rpm_l = 0.0
        self.rpm_r = 0.0
        self.duty_l = 0.0
        self.duty_r = 0.0

        self.head_i = 0.0
        self.head_ref = 0          # encoder difference we're holding
        self.hold_heading = True

        self._last = time.ticks_ms()
        self._last_l = 0
        self._last_r = 0

        # maneuver state
        self.maneuver = None       # None | "move" | "turn"
        self._man_target = 0       # counts
        self._man_l0 = 0
        self._man_r0 = 0
        self._man_sign = 1
        self._man_inside_ms = 0
        self._man_deadline = 0

    # --- commands ---
    def set_wheels(self, rpm_l, rpm_r):
        """Direct wheel speed targets. Cancels any maneuver."""
        self.maneuver = None
        self._set_targets(rpm_l, rpm_r)

    def set_twist(self, fwd, turn):
        """fwd/turn normalized -1..1. The joystick and arrow keys use this."""
        self.maneuver = None
        f = _clamp(fwd, -1.0, 1.0) * MAX_RPM
        t = _clamp(turn, -1.0, 1.0) * MAX_RPM * TURN_SIGN
        self._set_targets(f + t, f - t)

    def _set_targets(self, l, r):
        straight = abs(l - r) < 1.0 and abs(l) > 0.5
        if straight and not self.hold_heading:
            self.head_ref = self.enc_l.count - self.enc_r.count
            self.head_i = 0.0
        self.hold_heading = straight
        self.tgt_l = _clamp(l, -MAX_RPM, MAX_RPM)
        self.tgt_r = _clamp(r, -MAX_RPM, MAX_RPM)

    def stop(self):
        self.maneuver = None
        self.tgt_l = self.tgt_r = 0.0
        self.cmd_l = self.cmd_r = 0.0
        self.pi_l.reset()
        self.pi_r.reset()
        self.head_i = 0.0
        self.hold_heading = False
        self.duty_l = self.duty_r = 0.0
        self.left.stop()
        self.right.stop()

    # --- maneuvers ---
    def move_mm(self, mm, timeout_s=30):
        """Closed-loop straight run. Self-terminating."""
        counts = int(abs(mm) / MM_PER_COUNT)
        self._begin("move", counts, 1 if mm >= 0 else -1, timeout_s)
        self.head_ref = self.enc_l.count - self.enc_r.count
        self.head_i = 0.0
        self.hold_heading = True

    def turn_deg(self, deg, timeout_s=30):
        """Closed-loop spin in place. Positive = right, using real geometry."""
        counts = int(abs(deg) * COUNTS_PER_DEG)
        self._begin("turn", counts, 1 if deg >= 0 else -1, timeout_s)
        self.hold_heading = False

    def _begin(self, kind, counts, sign, timeout_s):
        self.maneuver = kind
        self._man_target = counts
        self._man_sign = sign
        self._man_l0 = self.enc_l.count
        self._man_r0 = self.enc_r.count
        self._man_inside_ms = 0
        self._man_deadline = time.ticks_add(time.ticks_ms(), int(timeout_s * 1000))
        self.pi_l.reset()
        self.pi_r.reset()

    def maneuver_progress(self):
        """0.0 -> 1.0, or None if no maneuver running."""
        if not self.maneuver or self._man_target <= 0:
            return None
        dl = abs(self.enc_l.count - self._man_l0)
        dr = abs(self.enc_r.count - self._man_r0)
        return min(1.0, ((dl + dr) / 2.0) / self._man_target)

    def _run_maneuver(self, now):
        dl = self.enc_l.count - self._man_l0
        dr = self.enc_r.count - self._man_r0

        if self.maneuver == "move":
            travelled = (abs(dl) + abs(dr)) / 2.0
        else:
            # spin: wheels counter-rotate, both contribute
            travelled = (abs(dl) + abs(dr)) / 2.0

        remaining = self._man_target - travelled

        if remaining <= ARRIVE_COUNTS:
            self._man_inside_ms += int(DT * 1000)
            if self._man_inside_ms >= ARRIVE_HOLD_MS:
                self.maneuver = None
                self._set_targets(0.0, 0.0)
                return
        else:
            self._man_inside_ms = 0

        if time.ticks_diff(now, self._man_deadline) > 0:
            self.maneuver = None
            self._set_targets(0.0, 0.0)
            return

        # trapezoid: full speed, then ease down over the last stretch
        speed = MANEUVER_RPM
        if remaining < DECEL_COUNTS:
            frac = remaining / DECEL_COUNTS
            speed = MANEUVER_MIN_RPM + (MANEUVER_RPM - MANEUVER_MIN_RPM) * frac
        if speed < MANEUVER_MIN_RPM:
            speed = MANEUVER_MIN_RPM

        s = self._man_sign
        if self.maneuver == "move":
            self.tgt_l = self.tgt_r = speed * s
        else:
            ts = s * TURN_SIGN              # same convention as set_twist
            self.tgt_l = speed * ts         # right turn: left wheel forward
            self.tgt_r = -speed * ts

    # --- update ---
    def update(self):
        now = time.ticks_ms()
        dt = time.ticks_diff(now, self._last) / 1000.0
        if dt < DT:
            return False
        self._last = now

        cl = self.enc_l.count
        cr = self.enc_r.count
        self.rpm_l = counts_to_rpm(cl - self._last_l, dt)
        self.rpm_r = counts_to_rpm(cr - self._last_r, dt)
        self._last_l, self._last_r = cl, cr

        if self.maneuver:
            self._run_maneuver(now)

        # slew the setpoints (soft start)
        step = RAMP_RPM_S * dt
        self.cmd_l = _approach(self.cmd_l, self.tgt_l, step)
        self.cmd_r = _approach(self.cmd_r, self.tgt_r, step)

        if abs(self.cmd_l) < 0.1 and abs(self.cmd_r) < 0.1:
            self.duty_l = self.duty_r = 0.0
            self.left.stop()
            self.right.stop()
            self.pi_l.reset()
            self.pi_r.reset()
            return True

        out_l = self.pi_l.step(self.cmd_l, self.rpm_l, dt)
        out_r = self.pi_r.step(self.cmd_r, self.rpm_r, dt)

        # heading hold, straight-line and 'move' maneuvers only
        if self.hold_heading:
            diff = (cl - cr) - self.head_ref
            self.head_i += KI_HEADING * diff * dt
            self.head_i = _clamp(self.head_i, -HEAD_I_CLAMP, HEAD_I_CLAMP)
            corr = _clamp(KP_HEADING * diff + self.head_i, -HEAD_CLAMP, HEAD_CLAMP)
            out_l -= corr
            out_r += corr
        else:
            self.head_i = 0.0

        self.duty_l = _clamp(out_l, -MAX_DUTY, MAX_DUTY)
        self.duty_r = _clamp(out_r, -MAX_DUTY, MAX_DUTY)
        self.left.drive(self.duty_l)
        self.right.drive(self.duty_r)
        return True

    # --- telemetry ---
    def heading_deg(self):
        diff = (self.enc_l.count - self.enc_r.count) - self.head_ref
        return (diff * MM_PER_COUNT) / TRACK_MM * 57.2958

    def telemetry(self):
        return {
            "evt": "telemetry",
            "rpm_l": round(self.rpm_l, 1), "rpm_r": round(self.rpm_r, 1),
            "tgt_l": round(self.cmd_l, 1), "tgt_r": round(self.cmd_r, 1),
            "count_l": self.enc_l.count, "count_r": self.enc_r.count,
            "duty_l": round(self.duty_l, 3), "duty_r": round(self.duty_r, 3),
            "head_deg": round(self.heading_deg(), 1),
            "man": self.maneuver,
            "man_pct": round((self.maneuver_progress() or 0) * 100),
        }


def _clamp(v, lo, hi):
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def _approach(cur, tgt, step):
    if abs(tgt - cur) <= step:
        return tgt
    return cur + (step if tgt > cur else -step)
