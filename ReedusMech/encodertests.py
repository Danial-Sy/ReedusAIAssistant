# encoder verify: drive forward at fixed duty, check counts match expected revs
from machine import Pin, PWM
import time

# Encoder setup
ENC_A = Pin(14, Pin.IN, Pin.PULL_UP)
ENC_B = Pin(15, Pin.IN, Pin.PULL_UP)

count = 0

def on_a(pin):
    global count
    if ENC_A.value() == ENC_B.value():
        count -= 1
    else:
        count += 1

def on_b(pin):
    global count
    if ENC_A.value() == ENC_B.value():
        count += 1
    else:
        count -= 1

ENC_A.irq(trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING, handler=on_a)
ENC_B.irq(trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING, handler=on_b)

# Motor setup
RPWM = PWM(Pin(10)); RPWM.freq(20000)
LPWM = PWM(Pin(11)); LPWM.freq(20000)

def stop():
    RPWM.duty_u16(0)
    LPWM.duty_u16(0)

def forward(duty_pct):
    LPWM.duty_u16(0)
    RPWM.duty_u16(int(duty_pct / 100 * 65535))

# at 50% duty on a 76 rpm motor with light load expect ~38 rpm = 0.63 rev/s, ~3.17 revs over 5s

DUTY = 50          # percent
RUN_SECONDS = 5

try:
    print("Settling at 0 for 1 s...")
    stop()
    time.sleep(1)

    # Reset count right before driving
    count = 0
    print(f"Driving forward at {DUTY}% for {RUN_SECONDS} s...")
    print("time_s,count")

    forward(DUTY)
    t0 = time.ticks_ms()
    next_print = t0

    while time.ticks_diff(time.ticks_ms(), t0) < RUN_SECONDS * 1000:
        now = time.ticks_ms()
        if time.ticks_diff(now, next_print) >= 100:   # print every 100 ms
            elapsed = time.ticks_diff(now, t0) / 1000
            print(f"{elapsed:.3f},{count}")
            next_print = now
        time.sleep_ms(5)

    stop()
    final_count = count
    print(f"\nFinal count after {RUN_SECONDS}s at {DUTY}% duty: {final_count}")

    # Reverse test to confirm sign flip
    print("\nNow reversing for 2 s to check sign...")
    count = 0
    LPWM.duty_u16(int(DUTY / 100 * 65535))
    RPWM.duty_u16(0)
    time.sleep(2)
    stop()
    print(f"Reverse count: {count} (should be negative)")

finally:
    stop()
