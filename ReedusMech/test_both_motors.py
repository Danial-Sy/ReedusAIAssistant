
from reedus_hw import (Motor, Encoder, counts_to_rpm,
                       M_L_RPWM, M_L_LPWM, M_L_ENC_A, M_L_ENC_B,
                       M_R_RPWM, M_R_LPWM, M_R_ENC_A, M_R_ENC_B)
from machine import Pin
import time

led = Pin("LED", Pin.OUT)

left   = Motor(M_L_RPWM, M_L_LPWM)
right  = Motor(M_R_RPWM, M_R_LPWM)
enc_L  = Encoder(M_L_ENC_A, M_L_ENC_B)
enc_R  = Encoder(M_R_ENC_A, M_R_ENC_B)

DUTY   = 0.40    # 40% test drive level
HOLD   = 2.0     # seconds held in each direction
SAMPLE = 0.25    # encoder sample period (s)


def test_channel(name, motor, enc):
    print("\n==== Testing %s wheel ====" % name)
    for label, duty in (("FORWARD", DUTY), ("REVERSE", -DUTY)):
        motor.stop(); enc.reset(); time.sleep(0.3)
        print("  %s at %d%% duty:" % (label, int(abs(duty) * 100)))
        motor.drive(duty)
        t0 = time.ticks_ms()
        last_t = t0
        last_c = enc.count
        while time.ticks_diff(time.ticks_ms(), t0) < int(HOLD * 1000):
            time.sleep(SAMPLE)
            now = time.ticks_ms()
            c = enc.count
            dt = time.ticks_diff(now, last_t) / 1000.0
            rpm = counts_to_rpm(c - last_c, dt)
            print("    t=%4.2fs  count=%8d  rpm=%6.1f" %
                  (time.ticks_diff(now, t0) / 1000.0, c, rpm))
            last_t, last_c = now, c
        motor.stop()
        total = enc.count
        # Direction sanity check
        if label == "FORWARD" and total < 0:
            print("    !! FORWARD gave NEGATIVE counts -> set sign=-1 on the %s encoder" % name)
        elif label == "REVERSE" and total > 0:
            print("    !! REVERSE gave POSITIVE counts -> set sign=-1 on the %s encoder" % name)
        else:
            print("    OK: %s %s direction sign is correct (net counts=%d)" % (name, label, total))
        time.sleep(0.5)
    motor.stop()


def test_together():
    print("\n==== Both wheels FORWARD together (robot should go straight) ====")
    enc_L.reset(); enc_R.reset()
    left.drive(DUTY); right.drive(DUTY)
    time.sleep(2.0)
    left.stop(); right.stop()
    cl, cr = enc_L.count, enc_R.count
    print("  left counts=%d   right counts=%d" % (cl, cr))
    if cl and cr:
        ratio = abs(cl) / abs(cr)
        print("  L/R count ratio = %.3f  (1.000 = perfectly matched wheels)" % ratio)
    print("  If one wheel spins the WRONG way for 'forward', set invert=True on that Motor.")


try:
    print("LED on -> Pico W alive")
    led.on()
    test_channel("LEFT", left, enc_L)
    test_channel("RIGHT", right, enc_R)
    test_together()
    print("\nAll tests complete.")
finally:
    left.stop(); right.stop(); led.off()
