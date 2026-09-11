# straight-line forward sanity check, run before any PID work.
# run from Thonny then unplug usb during the countdown (Pico keeps running on its own power).
# to actually see logs leave it plugged in with the wheels off the ground.
#
# countdown -> optional stiction kick -> ramp to DUTY, hold, ramp down -> 10 Hz log.
# the summary tells you what to fix: encoder sign (Encoder sign=-1), wheel direction
# (Motor invert=True), and the L/R mismatch the PID will exist to correct.
# everything is in try/finally so motors stop on any error or Ctrl-C.

from reedus_hw import make_drive, counts_to_rpm, COUNTS_PER_REV, RATED_RPM
from machine import Pin
import time

COUNTDOWN_S     = 15      # time to unplug the wire / put the robot down
DUTY            = 0.55    # heavy robot, start here and adjust
RUN_SECONDS     = 4.0     # how long to hold the drive
RAMP_S          = 0.5

KICK_ENABLED    = True    # brief high-duty pulse to break stiction
KICK_DUTY       = 0.85
KICK_MS         = 180

SAMPLE_HZ       = 10
WHEEL_DIAM_MM   = 65.0

led = Pin("LED", Pin.OUT)

# orientation (invert / encoder sign) comes from reedus_hw.py
left, right, enc_l, enc_r = make_drive()

MM_PER_REV = 3.14159 * WHEEL_DIAM_MM


def both(duty):
    left.drive(duty)
    right.drive(duty)


def stop_both():
    left.stop()
    right.stop()


def countdown():
    print("=" * 58)
    print("FORWARD TEST, duty %d%%, run %.1fs" % (int(DUTY * 100), RUN_SECONDS))
    print("=" * 58)
    print("UNPLUG THE USB CABLE NOW. Starting in %d seconds." % COUNTDOWN_S)
    print("(Ctrl-C any time to abort.)")
    for s in range(COUNTDOWN_S, 0, -1):
        # fast blink in the last 5 s so you know it's about to go
        if s <= 5:
            print("  %d..." % s)
            for _ in range(4):
                led.toggle(); time.sleep_ms(125)
        else:
            if s % 5 == 0:
                print("  %d..." % s)
            led.on(); time.sleep_ms(150)
            led.off(); time.sleep_ms(850)
    led.on()
    print("\nGO\n")


def ramp(from_duty, to_duty, seconds):
    steps = max(1, int(seconds * 50))
    for i in range(steps + 1):
        both(from_duty + (to_duty - from_duty) * i / steps)
        time.sleep_ms(int(seconds * 1000 / steps))


def run():
    countdown()

    enc_l.reset()
    enc_r.reset()

    if KICK_ENABLED:
        print("kick: %d%% for %d ms" % (int(KICK_DUTY * 100), KICK_MS))
        both(KICK_DUTY)
        time.sleep_ms(KICK_MS)

    ramp(KICK_DUTY if KICK_ENABLED else 0.0, DUTY, RAMP_S)

    print("\n  t(s)   L_cnt   R_cnt   L_rpm   R_rpm   dist_mm")
    print("  " + "-" * 48)

    period_ms = int(1000 / SAMPLE_HZ)
    t0 = time.ticks_ms()
    last_t = t0
    last_l = enc_l.count
    last_r = enc_r.count
    peak_l = peak_r = 0.0
    stalled = False

    while time.ticks_diff(time.ticks_ms(), t0) < int(RUN_SECONDS * 1000):
        time.sleep_ms(period_ms)
        now = time.ticks_ms()
        cl, cr = enc_l.count, enc_r.count
        dt = time.ticks_diff(now, last_t) / 1000.0
        rpm_l = counts_to_rpm(cl - last_l, dt)
        rpm_r = counts_to_rpm(cr - last_r, dt)
        elapsed = time.ticks_diff(now, t0) / 1000.0
        dist = ((abs(cl) + abs(cr)) / 2.0 / COUNTS_PER_REV) * MM_PER_REV

        print("  %5.2f  %7d %7d  %6.1f  %6.1f   %7.0f" %
              (elapsed, cl, cr, rpm_l, rpm_r, dist))

        if abs(rpm_l) > peak_l: peak_l = abs(rpm_l)
        if abs(rpm_r) > peak_r: peak_r = abs(rpm_r)

        # stall check after the first half-second
        if elapsed > 0.5 and abs(cl) < 50 and abs(cr) < 50 and not stalled:
            stalled = True
            print("  !! NO MOVEMENT at %d%% duty, stalled, or encoders not reading." % int(DUTY * 100))
            print("     Raise DUTY, check the battery under load, check encoder wiring.")

        last_t, last_l, last_r = now, cl, cr

    ramp(DUTY, 0.0, RAMP_S)
    stop_both()
    time.sleep(0.4)                      # let it coast to a stop before reading
    return enc_l.count, enc_r.count, peak_l, peak_r


def summary(cl, cr, peak_l, peak_r):
    print("\n" + "=" * 58)
    print("SUMMARY")
    print("=" * 58)
    rev_l = cl / COUNTS_PER_REV
    rev_r = cr / COUNTS_PER_REV
    print("  LEFT : %8d counts  %6.2f rev  peak %5.1f rpm" % (cl, rev_l, peak_l))
    print("  RIGHT: %8d counts  %6.2f rev  peak %5.1f rpm" % (cr, rev_r, peak_r))
    print("  distance travelled: ~%.0f mm" % (((abs(cl) + abs(cr)) / 2.0 / COUNTS_PER_REV) * MM_PER_REV))

    print("\n  --- checks ---")
    ok = True

    if abs(cl) < 100 and abs(cr) < 100:
        print("  FAIL: neither wheel moved. Bump DUTY (try 0.7), verify 12V under load.")
        return
    if abs(cl) < 100:
        print("  FAIL: LEFT wheel didn't move (right did). Check left driver/motor wiring.")
        ok = False
    if abs(cr) < 100:
        print("  FAIL: RIGHT wheel didn't move (left did). Check right driver/motor wiring.")
        ok = False

    if cl < 0:
        print("  FIX : LEFT counts went NEGATIVE on forward.")
        print("        If the wheel spun forward -> Encoder(..., sign=-1) for left.")
        print("        If the wheel spun backward -> Motor(..., invert=True) for left.")
        ok = False
    if cr < 0:
        print("  FIX : RIGHT counts went NEGATIVE on forward.")
        print("        If the wheel spun forward -> Encoder(..., sign=-1) for right.")
        print("        If the wheel spun backward -> Motor(..., invert=True) for right.")
        ok = False

    if abs(cl) > 100 and abs(cr) > 100:
        ratio = abs(cl) / abs(cr)
        drift = (abs(cl) - abs(cr)) / ((abs(cl) + abs(cr)) / 2.0) * 100
        print("  L/R ratio: %.3f  (1.000 = perfectly matched)" % ratio)
        if abs(drift) < 3:
            print("  GOOD: wheels within %.1f%%, drives near-straight open-loop." % abs(drift))
        elif abs(drift) < 12:
            print("  OK  : %.1f%% mismatch, veers toward the %s. Normal, PID will fix it."
                  % (abs(drift), "right" if abs(cl) > abs(cr) else "left"))
        else:
            print("  WARN: %.1f%% mismatch, that's a lot. Check for a binding wheel,"
                  % abs(drift))
            print("        dragging chassis, or a weak driver before blaming software.")

    avg_peak = (peak_l + peak_r) / 2.0
    expect = RATED_RPM * DUTY
    print("\n  peak RPM %.1f vs %.1f expected at %d%% of %d rpm rated"
          % (avg_peak, expect, int(DUTY * 100), RATED_RPM))
    if avg_peak < expect * 0.5:
        print("  NOTE: well under expected, heavy load, low battery, or duty too low.")
        print("        For PID, this tells you your usable duty floor is high.")

    if ok:
        print("\n  Ready for PID. Note the numbers above: the L/R mismatch is your")
        print("  error signal, and peak RPM at this duty sets your output scaling.")
    print("=" * 58)


try:
    cl, cr, pl, pr = run()
    summary(cl, cr, pl, pr)
except KeyboardInterrupt:
    print("\n\naborted by user")
finally:
    stop_both()
    led.off()
    print("\nmotors stopped.")
