# measures the plant so PID gains can be calculated instead of guessed.
# every forward burst is mirrored in reverse so the robot stays put - keep it tethered,
# the serial log is the whole point.
#
# phases:
#   1. deadband  - creep duty up until each wheel breaks loose (heavy robot + 100:1 gearbox,
#                  it's nowhere near zero and the PID has to know)
#   2. duty sweep - steady-state rpm at several duties. slope = plant gain Kv
#   3. step       - 100 Hz log of a duty step to get tau (63% rise)
# then prints suggested Kp/Ki and a starting heading gain. run test_forward.py first.

from reedus_hw import make_drive, counts_to_rpm, COUNTS_PER_REV, RATED_RPM
from machine import Pin
import time

COUNTDOWN_S   = 8

ON_STAND      = False

# phase 1, deadband creep
CREEP_START   = 0.10
CREEP_STEP    = 0.02
CREEP_DWELL   = 0.30      # s at each duty before checking for movement
CREEP_MAX     = 0.90
MOVE_COUNTS   = 60        # counts in one dwell that mean "it's turning"

# phase 2, duty sweep
SWEEP         = (0.35, 0.45, 0.55, 0.65, 0.80, 1.00)
DWELL_S       = 1.10      # hold at each duty
SETTLE_S      = 0.45

# phase 3, step response
STEP_DUTY     = 0.60
STEP_S        = 1.00
STEP_DT_MS    = 10        # 100 Hz logging

PAUSE_S       = 0.6       # between bursts, let it fully stop
CTRL_HZ       = 50        # the rate your PID loop will actually run at

led = Pin("LED", Pin.OUT)
left, right, enc_l, enc_r = make_drive()


def both(d):
    left.drive(d)
    right.drive(d)


def stop_both():
    left.stop()
    right.stop()


def rest():
    stop_both()
    time.sleep(PAUSE_S)


def countdown():
    print("=" * 62)
    print("PID CHARACTERIZATION, robot stays near its starting point")
    print("=" * 62)
    print("Keep the USB cable connected. Give it ~40cm of clear space.")
    print("Starting in %d s. Ctrl-C aborts." % COUNTDOWN_S)
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


def measure(duty, seconds, settle):
    """Drive at duty, return (rpm_l, rpm_r) averaged after the settle window."""
    both(duty)
    time.sleep(settle)
    cl0, cr0 = enc_l.count, enc_r.count
    t0 = time.ticks_ms()
    time.sleep(seconds - settle)
    dt = time.ticks_diff(time.ticks_ms(), t0) / 1000.0
    cl1, cr1 = enc_l.count, enc_r.count
    stop_both()
    return counts_to_rpm(cl1 - cl0, dt), counts_to_rpm(cr1 - cr0, dt)


def burst_pair(duty, seconds, settle):
    """Forward measurement, then an equal reverse burst to return home."""
    f = measure(duty, seconds, settle)
    time.sleep(PAUSE_S)
    if not ON_STAND:
        both(-duty)
        time.sleep(seconds)
        stop_both()
        time.sleep(PAUSE_S)
    return f


def phase_deadband():
    print("-" * 62)
    print("PHASE 1: deadband, lowest duty that actually turns each wheel")
    print("-" * 62)
    db_l = db_r = None
    duty = CREEP_START
    while duty <= CREEP_MAX and (db_l is None or db_r is None):
        enc_l.reset(); enc_r.reset()
        both(duty)
        time.sleep(CREEP_DWELL)
        cl, cr = abs(enc_l.count), abs(enc_r.count)
        stop_both()
        moved_l = cl >= MOVE_COUNTS
        moved_r = cr >= MOVE_COUNTS
        print("  duty %3d%%   L:%6d %s   R:%6d %s" %
              (int(duty * 100), cl, "MOVING" if moved_l else "  --  ",
               cr, "MOVING" if moved_r else "  --  "))
        if db_l is None and moved_l:
            db_l = duty
        if db_r is None and moved_r:
            db_r = duty
        # small reverse nudge so the creep doesn't walk the robot away
        if not ON_STAND and (moved_l or moved_r):
            both(-duty); time.sleep(CREEP_DWELL); stop_both()
        time.sleep(0.15)
        duty += CREEP_STEP
    rest()
    print("  -> LEFT  breaks loose at %s" % (("%d%%" % int(db_l * 100)) if db_l else "NEVER"))
    print("  -> RIGHT breaks loose at %s" % (("%d%%" % int(db_r * 100)) if db_r else "NEVER"))
    return db_l, db_r


def phase_sweep():
    print("")
    print("-" * 62)
    print("PHASE 2: steady-state RPM vs duty")
    print("-" * 62)
    print("  duty     L_rpm    R_rpm    diff    diff%")
    print("  " + "-" * 44)
    rows = []
    for d in SWEEP:
        rl, rr = burst_pair(d, DWELL_S, SETTLE_S)
        avg = (abs(rl) + abs(rr)) / 2.0
        diff = abs(rl) - abs(rr)
        pct = (diff / avg * 100.0) if avg > 1 else 0.0
        print("  %3d%%    %6.1f   %6.1f   %6.1f   %+5.1f%%" %
              (int(d * 100), rl, rr, diff, pct))
        if rl < -1 or rr < -1:
            print("       !! NEGATIVE rpm while driving forward, encoder sign is")
            print("          backwards. Flip ENC_L_SIGN / ENC_R_SIGN in reedus_hw.py")
            print("          BEFORE running any PID, or the loop will run away.")
        rows.append((d, abs(rl), abs(rr)))
    rest()
    return rows


def phase_step():
    print("")
    print("-" * 62)
    print("PHASE 3: step response at %d%% duty (100 Hz)" % int(STEP_DUTY * 100))
    print("-" * 62)
    samples = []
    enc_l.reset(); enc_r.reset()
    last_l, last_r = 0, 0
    t0 = time.ticks_ms()
    last_t = t0
    both(STEP_DUTY)
    while time.ticks_diff(time.ticks_ms(), t0) < int(STEP_S * 1000):
        time.sleep_ms(STEP_DT_MS)
        now = time.ticks_ms()
        cl, cr = enc_l.count, enc_r.count
        dt = time.ticks_diff(now, last_t) / 1000.0
        if dt <= 0:
            continue
        rl = counts_to_rpm(cl - last_l, dt)
        rr = counts_to_rpm(cr - last_r, dt)
        samples.append((time.ticks_diff(now, t0) / 1000.0, abs(rl), abs(rr)))
        last_t, last_l, last_r = now, cl, cr
    stop_both()
    time.sleep(PAUSE_S)
    if not ON_STAND:
        both(-STEP_DUTY); time.sleep(STEP_S); stop_both()
    rest()

    # steady state = mean of the last 30% of samples
    tail = samples[int(len(samples) * 0.7):]
    ss_l = sum(s[1] for s in tail) / len(tail)
    ss_r = sum(s[2] for s in tail) / len(tail)
    ss = (ss_l + ss_r) / 2.0

    # tau = time to reach 63.2% of steady state
    target = 0.632 * ss
    tau = None
    for t, a, b in samples:
        if (a + b) / 2.0 >= target:
            tau = t
            break

    print("  logged %d samples" % len(samples))
    print("  steady state: L %.1f rpm   R %.1f rpm   avg %.1f rpm" % (ss_l, ss_r, ss))
    if tau:
        print("  tau (63%% rise): %.3f s" % tau)
    else:
        print("  tau: never reached 63%%, step too short or wheels stalled")
    print("")
    print("  first 300 ms of the rise:")
    print("    t(s)    L_rpm   R_rpm")
    for t, a, b in samples[:30]:
        print("    %.2f   %6.1f  %6.1f" % (t, a, b))
    return ss, tau, ss_l, ss_r


def linfit(pts):
    """Least squares y = m*x + b. pts = [(x, y), ...]"""
    n = len(pts)
    if n < 2:
        return 0.0, 0.0
    sx = sum(p[0] for p in pts)
    sy = sum(p[1] for p in pts)
    sxx = sum(p[0] * p[0] for p in pts)
    sxy = sum(p[0] * p[1] for p in pts)
    den = n * sxx - sx * sx
    if den == 0:
        return 0.0, 0.0
    m = (n * sxy - sx * sy) / den
    b = (sy - m * sx) / n
    return m, b


def analyze(db_l, db_r, rows, ss, tau, ss_l, ss_r):
    print("")
    print("=" * 62)
    print("RESULTS")
    print("=" * 62)

    peak_ix = 0
    for i in range(len(rows)):
        if (rows[i][1] + rows[i][2]) > (rows[peak_ix][1] + rows[peak_ix][2]):
            peak_ix = i
    usable = rows[:peak_ix + 1]
    if len(usable) < len(rows):
        dropped = [int(d * 100) for d, l, r in rows[peak_ix + 1:]]
        print("  !! SATURATION: rpm FELL above %d%% duty, dropped %s from the fit"
              % (int(rows[peak_ix][0] * 100), dropped))
        print("     Battery sagging under load, or driver losing high-side drive.")
        print("     Cap your PID output at ~%d%% duty." % int(rows[peak_ix][0] * 100))

    pts_l = [(d, l) for d, l, r in usable if l > 1]
    pts_r = [(d, r) for d, l, r in usable if r > 1]
    kv_l, b_l = linfit(pts_l)
    kv_r, b_r = linfit(pts_r)
    kv = (kv_l + kv_r) / 2.0
    ff_off = (-(b_l + b_r) / 2.0) / kv if kv > 1 else 0.10
    print("  feedforward offset from fit: %.3f duty (zero-rpm intercept)" % ff_off)
    print("  max usable duty: %d%%" % int(rows[peak_ix][0] * 100))

    print("  plant gain (rpm per 1.0 duty):")
    print("    LEFT  Kv = %.1f    RIGHT Kv = %.1f    avg = %.1f" % (kv_l, kv_r, kv))
    print("    (no-load rated is %d rpm, loaded will be well under)" % RATED_RPM)

    if db_l and db_r:
        print("  deadband: L %d%%  R %d%%  -> feedforward offset %d%%"
              % (int(db_l * 100), int(db_r * 100), int(max(db_l, db_r) * 100)))

    mism = 0.0
    if ss_l > 1 and ss_r > 1:
        mism = (ss_l - ss_r) / ((ss_l + ss_r) / 2.0) * 100.0
        print("  wheel mismatch at step duty: %+.1f%% (%s wheel is faster)"
              % (mism, "left" if mism > 0 else "right"))

    print("")
    print("  --- suggested starting gains (velocity loop, per wheel) ---")
    T = 1.0 / CTRL_HZ
    if tau and kv > 1:
        lam = max(tau, 3 * T)              # IMC lambda: conservative = tau
        kp = tau / (kv * lam)
        ki = kp / tau
        print("    control loop rate : %d Hz  (T = %.3f s)" % (CTRL_HZ, T))
        print("    tau = %.3f s, Kv = %.1f rpm/duty" % (tau, kv))
        print("    Kp = %.5f   (duty per rpm of error)" % kp)
        print("    Ki = %.5f   (duty per rpm-second)" % ki)
        print("    Kd = 0       start here, D on encoder velocity is mostly noise")
        print("    integral clamp: +/- %.2f duty (anti-windup)" % 0.30)
    else:
        print("    not enough data, rerun with a longer STEP_S or higher STEP_DUTY")

    print("")
    print("  --- suggested heading correction (drive-straight loop) ---")
    if kv > 1:
        kp_head = 0.5 / max(1.0, kv)
        print("    Kp_heading = %.5f  (duty split per count of L-R difference)" % (kp_head / 100.0))
        print("    applies +corr to the slow wheel, -corr to the fast one")
        print("    clamp correction to +/- 0.15 duty so it can't spin the robot")
    print("")
    print("  Start at HALF these numbers, raise Kp until it oscillates, back off")
    print("  ~30%%, then add Ki until steady-state error dies. Standard procedure.")
    print("=" * 62)


try:
    countdown()
    db_l, db_r = phase_deadband()
    rows = phase_sweep()
    ss, tau, ss_l, ss_r = phase_step()
    analyze(db_l, db_r, rows, ss, tau, ss_l, ss_r)
except KeyboardInterrupt:
    print("\n\naborted by user")
finally:
    stop_both()
    led.off()
    print("\nmotors stopped.")
