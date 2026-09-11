# final encoder test
from machine import Pin, PWM
import time

COUNTS_PER_REV = 4400
DUTY = 50
RUN_SECONDS = 10

ENC_A = Pin(14, Pin.IN, Pin.PULL_UP)
ENC_B = Pin(15, Pin.IN, Pin.PULL_UP)
count = 0

def on_a(pin):
    global count
    if ENC_A.value() == ENC_B.value():
        count += 1
    else:
        count -= 1

def on_b(pin):
    global count
    if ENC_A.value() == ENC_B.value():
        count -= 1
    else:
        count += 1

ENC_A.irq(trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING, handler=on_a)
ENC_B.irq(trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING, handler=on_b)

RPWM = PWM(Pin(10)); RPWM.freq(20000)
LPWM = PWM(Pin(11)); LPWM.freq(20000)

def stop():
    RPWM.duty_u16(0); LPWM.duty_u16(0)

try:
    stop(); time.sleep(1)
    count = 0
    print("time_s,count,rpm")
    
    RPWM.duty_u16(int(DUTY / 100 * 65535))
    LPWM.duty_u16(0)
    
    t0 = time.ticks_ms()
    last_t = t0
    last_count = 0
    next_print = t0
    
    while time.ticks_diff(time.ticks_ms(), t0) < RUN_SECONDS * 1000:
        now = time.ticks_ms()
        if time.ticks_diff(now, next_print) >= 100:
            c = count
            dt = time.ticks_diff(now, last_t) / 1000
            rpm = ((c - last_count) / COUNTS_PER_REV) / dt * 60
            elapsed = time.ticks_diff(now, t0) / 1000
            print(f"{elapsed:.3f},{c},{rpm:.2f}")
            last_count = c
            last_t = now
            next_print = now
        time.sleep_ms(5)
    
    stop()
    print(f"\nFinal: {count} counts -> {count/COUNTS_PER_REV:.2f} revolutions")
    print(f"Average RPM: {(count/COUNTS_PER_REV) / (RUN_SECONDS/60):.2f}")

finally:
    stop()
