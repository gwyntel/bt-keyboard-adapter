# bt-keyboard-adapter

Turn any Linux machine into a BLE HID keyboard. Reads from a physical evdev keyboard device and forwards keystrokes over Bluetooth Low Energy HID-over-GATT to a paired phone or tablet.

Built on pure Python + dbus-python + BlueZ. No third-party BLE libraries required.

## What It Does

- Advertises as a BLE keyboard (GAP Appearance 0x03C1, HID Service 0x1812)
- Implements HID-over-GATT (HOGP) with a validated 8-byte keyboard report map
- Reads keypresses from `/dev/input/event*` (passive, no EVIOCGRAB — local X session keeps working)
- Forwards HID reports via GATT notifications with throttling + coalescing (30ms min interval) to prevent BlueZ ATT notification drops
- Just Works pairing (NoInputNoOutput capability) — no PIN or passkey needed
- Auto-repeat support (held keys repeat correctly)
- Optional tablet-mode handler (disable backlight/touchscreen when folded)

## Requirements

- Linux with BlueZ 5.50+ (tested on BlueZ 5.82, Debian 13)
- Python 3.9+
- `dbus-python`, `gobject` (PyGObject)
- Bluetooth adapter (built-in or USB dongle)
- A physical keyboard connected via USB or internal PS/2

```bash
sudo apt install python3-dbus python3-gi
```

## Installation

```bash
# Clone
git clone https://github.com/GwynTel/bt-keyboard-adapter.git
sudo cp -r bt-keyboard-adapter /opt/bt-keyboard-adapter

# Find your keyboard device
cat /proc/bus/input/devices | grep -B 2 -A 5 "keyboard"
# Look for the "Handlers=" line — note the event number (e.g., event0, event10)

# Install systemd service
sudo cp /opt/bt-keyboard-adapter/ble-keyboard.service /etc/systemd/system/
# Edit the ExecStart line to point to your keyboard's event device
sudo systemctl daemon-reload
sudo systemctl enable --now ble-keyboard
```

## Pairing

On your phone/tablet:

1. Open Bluetooth settings
2. Find "crackedbook-kbd" (or whatever alias you set)
3. Tap to pair — Just Works, no PIN needed
4. Type on the physical keyboard — characters appear on the phone

To change the advertised name:

```bash
bluetoothctl system-alias "My BLE Keyboard"
```

## Usage

```bash
# Run directly (foreground, with debug)
python3 ble_keyboard.py --input /dev/input/event0 --alias "My Keyboard" --debug

# Or as a systemd service
sudo systemctl start ble-keyboard
sudo journalctl -u ble-keyboard -f
```

### CLI Options

```
--input PATH     evdev keyboard input device (default: /dev/input/event0)
--alias NAME     Bluetooth advertised name (default: crackedbook-kbd)
--debug          Enable verbose HID report logging
```

## How It Works

### Report Map

Uses the proven Silabs/Nordic 8-byte keyboard-only HID report map — no Report ID bytes in the payload (BLE HID differs from USB HID in this regard). Consumer/media keys are omitted to ensure Android compatibility.

| Byte | Content |
|------|--------|
| 0 | Modifier keys (Ctrl/Shift/Alt/Gui × 2) |
| 1 | Reserved (0x00) |
| 2-7 | Up to 6 simultaneous keycodes |

### Notification Throttling

BlueZ 5.82 silently drops ATT notifications when the L2CAP socket buffer overflows (EAGAIN on `writev()`). This is fire-and-forget — no callback, no retry. The throttle enforces a 30ms minimum interval between notifications (matching typical BLE connection intervals) and coalesces pending reports so only the newest state is sent when the window opens.

### Pairing

`NoInputNoOutput` capability = Just Works bonding (LE Security Mode 1, Level 2, unauthenticated pairing with encryption). This is the standard for BLE HID keyboards. Higher capabilities (DisplayOnly, DisplayYesNo) cause Android to request passkey entry that a keyboard device can't complete.

### No EVIOCGRAB

The kernel input subsystem multicasts events to all open evdev clients. Xorg does not exclusively grab the keyboard, so a passive read of `/dev/input/event*` receives keystrokes alongside X. This means the local desktop session continues to function normally — no lockouts.

## Tablet Mode Handler

For 2-in-1 devices (e.g., Chromebook converted to Linux), `tablet-mode-handler.py` toggles backlight and touchscreen based on the tablet mode switch:

```bash
sudo cp tablet-mode-handler.py /opt/bt-keyboard-adapter/
# Run as a service or from /etc/rc.local
```

## Hardware

Tested on:
- HP Chromebook x360 (Debian 13, BlueZ 5.82, kernel 6.12)
- External USB mechanical keyboard via `/dev/input/event10`
- Google Pixel 8a (Android 16)

## Troubleshooting

**"Connects but no keys appear on phone"**
- Verify the report map has no Report ID bytes (BLE HID ≠ USB HID)
- Forget the device on the phone and re-pair — Android only parses the report map once at bond time
- Check `journalctl -u ble-keyboard` for `[HID report]` lines — if present, the pipeline works and the issue is GATT service resolution

**"Incorrect PIN / passcode"**
- Ensure pairing agent is `NoInputNoOutput`, not `DisplayOnly` or `DisplayYesNo`
- Remove the device on both sides (`bluetoothctl remove <MAC>` + forget on phone) before re-pairing

**"ServicesResolved: false"**
- GATT DB must be registered before advertising starts (the 500ms delay in the startup sequence handles this)
- Advertise HID Service UUID (0x1812) and Appearance (0x03C1) in the advertisement packet
- Clean re-pair on both ends

## License

MIT
