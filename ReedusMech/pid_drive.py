# closed-loop straight-line drive test. two loops stacked:
#   1. per-wheel velocity PI - feedforward does most of the work, PI trims the rest
#   2. heading P on the L-R encoder difference - this is what kills the open-loop drift
#
# gains come from test_pid_characterize.py (Kv=104, b=-9.8 -> 0.095 offset, tau=0.081s).
# IMC gives Kp=1/Kv, Ki=Kp/tau; halved here. raise until it oscillates then back off ~30%.
# duty is capped at 85%: 100% gave LESS speed than 80% (45 vs 72 rpm), so never go there.
# drives forward, pauses, returns, so the usb cable survives.

from reedus_hw import make_drive, counts_to_rpm, COUNTS_PER_REV
from machine import Pin
import time

TARGET_RPM   = 40.0     # cruise speed per wheel
RUN_S        = 3.0      # seconds per leg
COUNTDOWN_S  = 8
RETURN_LEG   = True     # drive back to roughly where it started

CTRL_HZ      = 50
DT           = 1.0 / CTRL_HZ

# --- plant model (measured) ---
KV           = 104.0    # rpm per unit duty
FF_OFFSET    = 0.095    # duty needed before the wheels turn at all
MAX_DUTY     = 0.85     # hard cap, above this the rig loses speed

# --- velocity loop (start at half the computed values) ---
KP           = 0.005    # computed 0.0096
KI           = 0.060    # computed 0.119
KD           = 0.0
I_CLAMP      = 0.30     # anti-windup, in duty units

KP_HEADING   = 0.0002   # duty split per count of (left - right)
KI_HEADING   = 0.00015  # duty split per count-second
HEAD_I_CLAMP = 0.08
HEAD_CLAMP   = 0.15     # never let heading correction dominate

RAMP_S       = 0.35

TRACK_MM     = 150.0
WHEEL_DIA_MM = 65.0

LOG_HZ       = 10

led = Pin("LED", Pin.OUT)
left, right, enc_l, enc_r = make_drive()

MM_PER_COUNT = (3.14159 * WHEEL_DIA_MM) / COUNTS_PER_REV


class PI:
    """Velocity PI with feedforward and anti-windup."""

    def __init__(self):
        self.i = 0.0
        self.prev_err = 0.0

    def reset(self):
        self.i = 0.0
        self.prev_err = 0.0

    def step(self, target_rpm, actual_rpm, dt):
        err = target_rpm - actual_rpm

        ff = 0.0
        if target_rpm > 0.1:
            ff = FF_OFFSET + target_rpm / KV
        elif target_rpm < -0.1:
            ff = -FF_OFFSET + target_rpm / KV

        p = KP * err
        d = KD * (err - self.prev_err) / dt if dt > 0 else 0.0
        self.prev_err = err

        out = ff + p + self.i + d
        if abs(out) < MAX_DUTY:
            self.i += KI * err * dt
            if self.i > I_CLAMP:
                self.i = I_CLAMP
            elif self.i < -I_CLAMP:
                self.i = -I_CLAMP

        out = ff + p + self.i + d
        if out > MAX_DUTY:
            out = MAX_DUTY
        elif out < -MAX_DUTY:
            out = -MAX_DUTY
        return out, err


pi_l = PI()
pi_r = PI()


def countdown():
    print("=" * 62)
    print("PID DRIVE, target %.0f rpm/wheel, %.1fs per leg" % (TARGET_RPM, RUN_S))
    print("=" * 62)
    print("Kp=%.4f  Ki=%.4f  Kv=%.0f  ff_offset=%.3f  max_duty=%.2f"
          % (KP, KI, KV, FF_OFFSET, MAX_DUTY))
    print("Keep the cable connected. Starting in %d s." % COUNTDOWN_S)
    for s in range(COUNTDOWN_S, 0, -1):
        if s <= 3:
            print("  %d..." % s)
            for _ in range(4):
                led.toggle(); time.sleep_ms(125)
        else:
            led.on(); time.sleep_ms(150)
            led.off(); time.sleep_ms(850)
    led.on()
    print("")


def leg(target_rpm, seconds, label):
    print("-" * 62)
    print("%s at %+.0f rpm" % (label, target_rpm))
    print("  t(s)  tgtL  rpmL  rpmR   dutyL  dutyR   L-R cnt  head")
    print("  " + "-" * 56)

    pi_l.reset(); pi_r.reset()
    enc_l.reset(); enc_r.reset()
    head_i = 0.0

    t_start = time.ticks_ms()
    last_t = t_start
    last_l, last_r = 0, 0
    next_log = t_start
    err_sum = 0.0
    err_n = 0
    max_head = 0.0

    while time.ticks_diff(time.ticks_ms(), t_start) < int(seconds * 1000):
        now = time.ticks_ms()
        dt = time.ticks_diff(now, last_t) / 1000.0
        if dt < DT:
            time.sleep_ms(1)
            continue

        cl, cr = enc_l.count, enc_r.count
        rpm_l = counts_to_rpm(cl - last_l, dt)
        rpm_r = counts_to_rpm(cr - last_r, dt)
        last_t, last_l, last_r = now, cl, cr

        elapsed = time.ticks_diff(now, t_start) / 1000.0
        ramp = elapsed / RAMP_S if elapsed < RAMP_S else 1.0
        tgt_now = target_rpm * ramp

        out_l, err_l = pi_l.step(tgt_now, rpm_l, dt)
        out_r, err_r = pi_r.step(tgt_now, rpm_r, dt)

        diff = cl - cr
        head_i += KI_HEADING * diff * dt
        if head_i > HEAD_I_CLAMP:
            head_i = HEAD_I_CLAMP
        elif head_i < -HEAD_I_CLAMP:
            head_i = -HEAD_I_CLAMP

        corr = KP_HEADING * diff + head_i
        if corr > HEAD_CLAMP:
            corr = HEAD_CLAMP
        elif corr < -HEAD_CLAMP:
            corr = -HEAD_CLAMP

        out_l -= corr
        out_r += corr

        if out_l > MAX_DUTY: out_l = MAX_DUTY
        elif out_l < -MAX_DUTY: out_l = -MAX_DUTY
        if out_r > MAX_DUTY: out_r = MAX_DUTY
        elif out_r < -MAX_DUTY: out_r = -MAX_DUTY

        left.drive(out_l)
        right.drive(out_r)

        err_sum += abs(err_l) + abs(err_r)
        err_n += 2

        head_deg = (diff * MM_PER_COUNT) / TRACK_MM * 57.2958
        if abs(head_deg) > abs(max_head):
            max_head = head_deg

        if time.ticks_diff(now, next_log) >= 0:
            print("  %.2f  %4.0f  %5.1f %5.1f   %+5.2f  %+5.2f   %+7d  %+5.1f"
                  % (time.ticks_diff(now, t_start) / 1000.0, target_rpm,
                     rpm_l, rpm_r, out_l, out_r, diff, head_deg))
            next_log = time.ticks_add(now, int(1000 / LOG_HZ))

    left.stop(); right.stop()

    cl, cr = enc_l.count, enc_r.count
    diff = cl - cr
    head_deg = (diff * MM_PER_COUNT) / TRACK_MM * 57.2958
    dist = ((abs(cl) + abs(cr)) / 2.0) * MM_PER_COUNT
    mean_err = err_sum / err_n if err_n else 0.0

    print("  " + "-" * 56)
    print("  distance ~%.0f mm   final L-R = %d counts   heading err %.1f deg"
          % (dist, diff, head_deg))
    print("  mean |velocity error| = %.1f rpm   peak heading %.1f deg"
          % (mean_err, max_head))
    return mean_err, head_deg


def verdict(err_f, head_f):
    print("")
    print("=" * 62)
    print("TUNING NOTES")
    print("=" * 62)
    if err_f > TARGET_RPM * 0.25:
        print("  Velocity error is large (%.1f rpm). Raise KP toward 0.010." % err_f)
    elif err_f > TARGET_RPM * 0.10:
        print("  Velocity tracking is okay (%.1f rpm off). Nudge KI up a little." % err_f)
    else:
        print("  Velocity tracking is good (%.1f rpm mean error)." % err_f)

    if abs(head_f) > 8:
        print("  Heading drift %.1f deg is high, raise KP_HEADING to 0.0004." % head_f)
        print("  If it stays put no matter the gain, the cause is MECHANICAL:")
        print("  encoders can't see wheel slip or unequal wheel diameter.")
    elif abs(head_f) > 3:
        print("  Heading drift %.1f deg, try KP_HEADING 0.0003." % head_f)
    else:
        print("  Heading held within %.1f deg. That's the drift solved." % head_f)
    print("")
    print("  If the robot buzzes/vibrates at speed, KP is too high, cut it 30%.")
    print("  If it creeps to speed slowly then overshoots, KI is too high.")
    print("=" * 62)


try:
    countdown()
    err_f, head_f = leg(TARGET_RPM, RUN_S, "FORWARD")
    time.sleep(1.0)
    if RETURN_LEG:
        leg(-TARGET_RPM, RUN_S, "RETURN")
    verdict(err_f, head_f)
except KeyboardInterrupt:
    print("\n\naborted by user")
finally:
    left.stop(); right.stop()
    led.off()
    print("\nmotors stopped.")
