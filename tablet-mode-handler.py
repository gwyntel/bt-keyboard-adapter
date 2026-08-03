#!/usr/bin/env python3
"""Tablet mode handler for crackedbook Chromebook.
Monitors SW_TABLET_MODE switch events and disables/enables
backlight and touchscreen accordingly.
"""
import os
import select
import struct
import sys
import time

BL_DIR = "/sys/class/backlight/intel_backlight"
SWITCH_DEV = "/dev/input/event5"  # Tablet Mode Switch
TOUCH_DRV_PATH = "/sys/bus/i2c/drivers/i2c_hid_acpi"
TOUCH_DEV_NAME = "i2c-GDIX0000:00"

EV_SW = 0x05
SW_TABLET_MODE = 0x01


def get_saved_brightness():
    try:
        with open(f"{BL_DIR}/brightness") as f:
            return int(f.read().strip())
    except Exception:
        return None


def set_backlight(on: bool, saved_brightness: int):
    """on=True restores saved brightness, on=False sets to 0."""
    try:
        val = str(saved_brightness) if on else "0"
        with open(f"{BL_DIR}/brightness", "w") as f:
            f.write(val)
    except Exception as e:
        print(f"  backlight error: {e}", file=sys.stderr)


def set_touchscreen(enabled: bool):
    """Bind or unbind the touchscreen from i2c_hid_acpi driver."""
    try:
        if enabled:
            with open(f"{TOUCH_DRV_PATH}/bind", "w") as f:
                f.write(TOUCH_DEV_NAME)
        else:
            with open(f"{TOUCH_DRV_PATH}/unbind", "w") as f:
                f.write(TOUCH_DEV_NAME)
    except FileNotFoundError:
        pass  # driver path may not exist, or device already unbound
    except Exception as e:
        print(f"  touchscreen error: {e}", file=sys.stderr)


def apply_state(tablet: bool, saved_brightness: int):
    if tablet:
        set_backlight(False, saved_brightness)
        set_touchscreen(False)
        print(f"[{time.strftime('%H:%M:%S')}] Tablet mode ON  — backlight off, touchscreen disabled")
    else:
        set_backlight(True, saved_brightness)
        set_touchscreen(True)
        print(f"[{time.strftime('%H:%M:%S')}] Tablet mode OFF — backlight restored, touchscreen enabled")


def main():
    saved = get_saved_brightness()
    if saved is None:
        print("FATAL: cannot read backlight brightness", file=sys.stderr)
        sys.exit(1)

    fd = os.open(SWITCH_DEV, os.O_RDONLY | os.O_NONBLOCK)

    # Determine initial state — try to read a recent SW_TABLET_MODE event
    in_tablet = False  # safe default
    try:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            data = os.read(fd, 24)
            if len(data) >= 24:
                _, _, etype, code, value = struct.unpack("llHHi", data)
                if etype == EV_SW and code == SW_TABLET_MODE:
                    in_tablet = bool(value)
    except Exception:
        pass

    apply_state(in_tablet, saved)
    print(f"[{time.strftime('%H:%M:%S')}] Initial state: tablet_mode={'ON' if in_tablet else 'OFF'}, ready")

    # Main loop: poll for switch events
    while True:
        try:
            r, _, _ = select.select([fd], [], [], 1.0)
            if not r:
                continue
            data = os.read(fd, 24)
            if len(data) < 24:
                continue
            _, _, etype, code, value = struct.unpack("llHHi", data)
            if etype == EV_SW and code == SW_TABLET_MODE:
                new_state = bool(value)
                if new_state != in_tablet:
                    in_tablet = new_state
                    apply_state(in_tablet, saved)
        except KeyboardInterrupt:
            # Restore laptop mode on exit
            apply_state(False, saved)
            break
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            time.sleep(1)

    os.close(fd)


if __name__ == "__main__":
    main()
