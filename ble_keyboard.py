#!/usr/bin/env python3
"""
BLE HID Keyboard Adapter (HID over GATT peripheral) for crackedbook.

Exposes the Chromebook as a Bluetooth Low Energy HID keyboard device using
BlueZ' GATT peripheral (GattManager1 + LEAdvertisingManager1) over D-Bus.
Input from a physical keyboard (/dev/input/event*) is forwarded as HID
Report notifications to any connected central (e.g. an iPad running iOS).

Pure stdlib + dbus-python — no third-party BLE libraries required.

Usage:
    python3 ble_keyboard.py --input /dev/input/event0 [--alias 'crackedbook kbd']
                            [--device-event-only]

Requirements:
    - BlueZ >= 5.50 (needs GattManager1 / LEAdvertisingManager1)
    - python3-dbus (Debian package python3-dbus, automatically present on desktop)
    - Write access to /dev/input/event*  ->  run as root, or add user to 'input' group
    - The Bluetooth adapter must advertise on an LE-capable controller.
"""

import argparse
import dbus
import dbus.exceptions
import dbus.mainloop.glib
import dbus.service
import errno
import fcntl
import os
import signal
import struct
import sys
import time

from gi.repository import GLib

# MUST be called at module level — @dbus.service.method decorators are evaluated
# at class-definition time, BEFORE main() runs. Without this, the Agent1 methods
# silently fail to register and pairing breaks.
dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

# ---------------------------------------------------------------------------
# Bluetooth / GATT constants
# ---------------------------------------------------------------------------
BLE_UUID_GAP = "00001800-0000-1000-8000-00805f9b34fb"
BLE_UUID_GATT = "00001801-0000-1000-8000-00805f9b34fb"
BLE_UUID_HID = "00001812-0000-1000-8000-00805f9b34fb"
BLE_UUID_CHR_HID_REPORT_MAP = "00002a4b-0000-1000-8000-00805f9b34fb"
BLE_UUID_CHR_HID_INFO = "00002a4a-0000-1000-8000-00805f9b34fb"
BLE_UUID_CHR_HID_CONTROL_POINT = "00002a4c-0000-1000-8000-00805f9b34fb"
BLE_UUID_CHR_HID_REPORT = "00002a4d-0000-1000-8000-00805f9b34fb"
BLE_UUID_CHR_PROTOCOL_MODE = "00002a4e-0000-1000-8000-00805f9b34fb"
BLE_UUID_CHR_PNP_ID = "00002a50-0000-1000-8000-00805f9b34fb"
BLE_UUID_APPEARANCE = "00002a01-0000-1000-8000-00805f9b34fb"
BLE_UUID_DEVICE_NAME = "00002a00-0000-1000-8000-00805f9b34fb"

SERVICE_APP_PATH = "/com/crackedbook/kbd"
SERVICE_PATH = "/com/crackedbook/kbd/service"
ADV_PATH = "/com/crackedbook/kbd/advertisement"
AGENT_PATH = "/com/crackedbook/kbd/agent"

# Well-known HID-over-GATT Report Map for a boot keyboard with media keys.
REPORT_MAP = bytes(
    [
        0x05, 0x01,  # Usage Page (Generic Desktop)
        0x09, 0x06,  # Usage (Keyboard)
        0xA1, 0x01,  # Collection (Application)
        # Modifier byte (8 modifier keys as bits)
        0x05, 0x07,  #   Usage Page (Key Codes)
        0x19, 0xE0,  #   Usage Minimum (224)
        0x29, 0xE7,  #   Usage Maximum (231)
        0x15, 0x00,  #   Logical Minimum (0)
        0x25, 0x01,  #   Logical Maximum (1)
        0x75, 0x01,  #   Report Size (1)
        0x95, 0x08,  #   Report Count (8)
        0x81, 0x02,  #   Input (Data, Variable, Absolute) -- modifier byte
        # Reserved byte
        0x95, 0x01,  #   Report Count (1)
        0x75, 0x08,  #   Report Size (8)
        0x81, 0x03,  #   Input (Constant) -- reserved
        # LED output report (5 LEDs + 3 padding bits)
        0x95, 0x05,  #   Report Count (5)
        0x75, 0x01,  #   Report Size (1)
        0x05, 0x08,  #   Usage Page (LEDs)
        0x19, 0x01,  #   Usage Minimum (1)
        0x29, 0x05,  #   Usage Maximum (5)
        0x91, 0x02,  #   Output (Data, Variable, Absolute)
        0x95, 0x01,  #   Report Count (1)
        0x75, 0x03,  #   Report Size (3)
        0x91, 0x03,  #   Output (Constant)
        # 6-byte keycodes (no Report ID — BLE HID does not use leading ID bytes)
        0x95, 0x06,  #   Report Count (6)
        0x75, 0x08,  #   Report Size (8)
        0x15, 0x00,  #   Logical Minimum (0)
        0x25, 0x65,  #   Logical Maximum (101)
        0x05, 0x07,  #   Usage Page (Key Codes)
        0x19, 0x00,  #   Usage Minimum (0)
        0x29, 0x65,  #   Usage Maximum (101)
        0x81, 0x00,  #   Input (Data, Array) -- 6 keycodes
        0xC0,  # End Collection
    ]
)

# HID Information value: bcdHID=1.11, country=0, flags: normally connectable
HID_INFO = bytes([0x11, 0x01, 0x00, 0x01])

# PnP information, compatible with a generic USB keyboard
PNP_ID = bytes([0x01, 0x05, 0xAC, 0x01, 0x34, 0x12, 0x00, 0x01])

# Appearance for HID keyboard = 961 (0x03C1)
APPEARANCE_KEYBOARD = 0x03C1


# ---------------------------------------------------------------------------
# D-Bus exception helper for GATT characteristic permission errors
# ---------------------------------------------------------------------------
class InvalidArgsException(dbus.exceptions.DBusException):
    _dbus_error_name = "org.freedesktop.DBus.Error.InvalidArgs"


class NotSupportedException(dbus.exceptions.DBusException):
    _dbus_error_name = "org.bluez.Error.NotSupported"


class FailedException(dbus.exceptions.DBusException):
    _dbus_error_name = "org.bluez.Error.Failed"


# ---------------------------------------------------------------------------
# GATT Service, Characteristics
# ---------------------------------------------------------------------------
# Low-level D-Bus objects
# ---------------------------------------------------------------------------
class Application(dbus.service.Object):
    def __init__(self, bus):
        self.path = SERVICE_APP_PATH
        self.services = []
        dbus.service.Object.__init__(self, bus, self.path)

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def add_service(self, service):
        self.services.append(service)

    @dbus.service.method(
        "org.freedesktop.DBus.ObjectManager", in_signature="", out_signature="a{oa{sa{sv}}}"
    )
    def GetManagedObjects(self):
        response = {}
        for service in self.services:
            response[service.get_path()] = service.get_properties()
            chrs = service.get_characteristics()
            for ch in chrs:
                response[ch.get_path()] = ch.get_properties()
                descs = ch.get_descriptors()
                for desc in descs:
                    response[desc.get_path()] = desc.get_properties()
        return response


class Service(dbus.service.Object):
    PATH_BASE = SERVICE_PATH

    def __init__(self, bus, index, uuid, primary):
        self.path = self.PATH_BASE + "/service" + str(index)
        self.bus = bus
        self.uuid = uuid
        self.primary = primary
        self.characteristics = []
        dbus.service.Object.__init__(self, bus, self.path)

    def get_properties(self):
        return {
            "org.bluez.GattService1": {
                "UUID": self.uuid,
                "Primary": dbus.Boolean(self.primary),
                "Characteristics": dbus.Array(
                    [dbus.ObjectPath(c.get_path()) for c in self.characteristics],
                    signature="o",
                ),
            }
        }

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def add_characteristic(self, characteristic):
        self.characteristics.append(characteristic)

    def get_characteristics(self):
        return self.characteristics


class Characteristic(dbus.service.Object):
    def __init__(self, bus, index, uuid, flags, service):
        self.path = service.path + "/char" + str(index)
        self.bus = bus
        self.uuid = uuid
        self.service = service
        self.flags = flags
        self.descriptors = []
        self.notifying = False
        self._value = dbus.Array([], signature="y")
        dbus.service.Object.__init__(self, bus, self.path)

    def get_properties(self):
        return {
            "org.bluez.GattCharacteristic1": {
                "UUID": self.uuid,
                "Service": self.service.get_path(),
                "Flags": self.flags,
                "Descriptors": dbus.Array(
                    [dbus.ObjectPath(d.get_path()) for d in self.descriptors],
                    signature="o",
                ),
            }
        }

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def add_descriptor(self, descriptor):
        self.descriptors.append(descriptor)

    def get_descriptors(self):
        return self.descriptors

    @dbus.service.method(
        "org.bluez.GattCharacteristic1", in_signature="aya{sv}", out_signature=""
    )
    def WriteValue(self, value, options):
        self.WriteValueInternal(dbus.ByteArray(value), options)

    def WriteValueInternal(self, value, options):
        raise NotSupportedException()

    @dbus.service.method(
        "org.bluez.GattCharacteristic1", in_signature="a{sv}", out_signature="ay"
    )
    def ReadValue(self, options):
        return self.ReadValueInternal(options)

    def ReadValueInternal(self, options):
        raise NotSupportedException

    @dbus.service.method(
        "org.bluez.GattCharacteristic1", in_signature="", out_signature=""
    )
    def StartNotify(self):
        self.notifying = True

    @dbus.service.method(
        "org.bluez.GattCharacteristic1", in_signature="", out_signature=""
    )
    def StopNotify(self):
        self.notifying = False

    @dbus.service.signal("org.freedesktop.DBus.Properties", signature="sa{sv}as")
    def PropertiesChanged(self, interface, changed, invalidated):
        pass

    def send_notification(self, data):
        if not self.notifying:
            return
        value = dbus.Array([dbus.Byte(b) for b in data], signature="y")
        self.PropertiesChanged(
            "org.bluez.GattCharacteristic1", {"Value": value}, []
        )

    def notify_update(self, data):
        """Emit PropertiesChanged so BlueZ forwards a notification to the peer."""
        if not self.notifying:
            return
        # CRITICAL: BlueZ expects a Python list of dbus.Byte, NOT dbus.Array.
        # The D-Bus type system auto-infers 'ay' from a list of bytes,
        # but dbus.Array(signature="y") can produce variant wrapping issues
        # in the {sv} dict that PropertiesChanged expects.
        value = [dbus.Byte(b) for b in data]
        self._value = value
        self.PropertiesChanged(
            "org.bluez.GattCharacteristic1",
            {"Value": value},
            [],
        )


class Descriptor(dbus.service.Object):
    def __init__(self, bus, index, uuid, flags, characteristic):
        self.path = characteristic.path + "/desc" + str(index)
        self.bus = bus
        self.uuid = uuid
        self.characteristic = characteristic
        self.flags = flags
        dbus.service.Object.__init__(self, bus, self.path)

    def get_properties(self):
        return {
            "org.bluez.GattDescriptor1": {
                "UUID": self.uuid,
                "Characteristic": self.characteristic.get_path(),
                "Flags": self.flags,
            }
        }

    def get_path(self):
        return dbus.ObjectPath(self.path)

    @dbus.service.method(
        "org.bluez.GattDescriptor1", in_signature="aya{sv}", out_signature=""
    )
    def WriteValue(self, value, options):
        raise NotSupportedException()

    @dbus.service.method(
        "org.bluez.GattDescriptor1", in_signature="a{sv}", out_signature="ay"
    )
    def ReadValue(self, options):
        return dbus.Array(
            [dbus.Byte(b) for b in self.ReadValueInternal(options)], signature="y"
        )

    def ReadValueInternal(self, options):
        raise NotSupportedException


# ---------------------------------------------------------------------------
# The concrete HID characteristics
# ---------------------------------------------------------------------------
class ProtocolModeCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        super().__init__(bus, index, BLE_UUID_CHR_PROTOCOL_MODE, ["read"], service)
        self.value = [dbus.Byte(1)]  # Report Protocol (default is report mode)

    def ReadValueInternal(self, options):
        return self.value

    def WriteValueInternal(self, value, options):
        val = int.from_bytes(bytes(value), "little")
        if val not in (0, 1):
            raise InvalidArgsException()
        self.value = [dbus.Byte(val)]


class ReportMapCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        super().__init__(bus, index, BLE_UUID_CHR_HID_REPORT_MAP, ["read"], service)
        self.value = [dbus.Byte(b) for b in REPORT_MAP]

    def ReadValueInternal(self, options):
        return self.value


class HIDInformationCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        super().__init__(bus, index, BLE_UUID_CHR_HID_INFO, ["read"], service)
        self.value = [dbus.Byte(b) for b in HID_INFO]

    def ReadValueInternal(self, options):
        return self.value


class HIDControlPointCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        super().__init__(bus, index, BLE_UUID_CHR_HID_CONTROL_POINT, ["write"], service)

    def WriteValueInternal(self, value, options):
        val = int.from_bytes(bytes(value), "little")
        # Only SUSPEND (0) is defined; other values are reserved
        if val != 0:
            raise NotSupportedException()


class ReportCharacteristic(Characteristic):
    """Keyboard input report (notify)."""

    def __init__(self, bus, index, service, report_type="input"):
        flags_enable_notify = ["read", "notify"]
        super().__init__(
            bus, index, BLE_UUID_CHR_HID_REPORT, flags_enable_notify, service
        )
        self.report_id = dbus.Byte(0)
        self.report_type = report_type
        # Initial empty keyboard report
        self.value = [dbus.Byte(0)] * 8
        # Report Reference descriptor
        self.ccd = None
        self.report_ref = ReportReferenceDescriptor(bus, 0, self)

    def ReadValueInternal(self, options):
        return self.value

    def WriteValueInternal(self, value, options):
        self.value = [dbus.Byte(b) for b in bytes(value)]

    def set_hid_report(self, data):
        """Set the report value (8-byte keyboard report) and notify peer if subscribed."""
        self.value = [dbus.Byte(b) for b in data]
        self.notify_update(data)


class ReportReferenceDescriptor(Descriptor):
    """CCD-style report reference (Report ID + Report Type)."""

    def __init__(self, bus, index, characteristic):
        super().__init__(
            bus, index, "00002908-0000-1000-8000-00805f9b34fb", ["read"], characteristic
        )
        # Report ID = 0, Report Type = 1 (Input)
        self.value = [dbus.Byte(0), dbus.Byte(1)]

    def ReadValueInternal(self, options):
        return self.value


class CCCDDescriptor(Descriptor):
    """Client Characteristic Configuration Descriptor for notifications."""

    def __init__(self, bus, index, characteristic):
        super().__init__(
            bus, index, "00002902-0000-1000-8000-00805f9b34fb", ["read", "write"],
            characteristic,
        )
        self.value = [dbus.Byte(0x00), dbus.Byte(0x00)]

    def ReadValueInternal(self, options):
        return self.value

    def WriteValueInternal(self, value, options):
        self.value = bytes(value)
        self.characteristic.notifying = self.value[0] == 0x01


class PNPCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        super().__init__(bus, index, BLE_UUID_CHR_PNP_ID, ["read"], service)
        self.value = [dbus.Byte(b) for b in PNP_ID]

    def ReadValueInternal(self, options):
        return self.value


class AppearanceCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        super().__init__(bus, index, BLE_UUID_APPEARANCE, ["read"], service)
        self.value = [
            dbus.Byte(APPEARANCE_KEYBOARD & 0xFF),
            dbus.Byte((APPEARANCE_KEYBOARD >> 8) & 0xFF),
        ]

    def ReadValueInternal(self, options):
        return self.value


class DeviceNameCharacteristic(Characteristic):
    def __init__(self, bus, index, service, name):
        super().__init__(bus, index, BLE_UUID_DEVICE_NAME, ["read"], service)
        self.value = [dbus.Byte(b) for b in name.encode("utf-8")]

    def ReadValueInternal(self, options):
        return self.value


# ---------------------------------------------------------------------------
# LE Advertisement
# ---------------------------------------------------------------------------
class Advertisement(dbus.service.Object):
    def __init__(self, bus, index, advertising_type, alias):
        self.path = ADV_PATH
        self.bus = bus
        self.ad_type = advertising_type
        self.service_uuids = [BLE_UUID_HID]
        self.manufacturer_data = None
        self.solicit_uuids = None
        self.service_data = None
        self.include_tx_power = dbus.Boolean(True)
        self.discoverable = dbus.Boolean(True)
        self.local_name = alias
        dbus.service.Object.__init__(self, bus, self.path)

    def get_properties(self):
        properties = dict()
        properties["Type"] = self.ad_type
        properties["ServiceUUIDs"] = dbus.Array(self.service_uuids, signature="s")
        properties["LocalName"] = self.local_name
        properties["Appearance"] = dbus.UInt16(APPEARANCE_KEYBOARD)
        properties["IncludeTxPower"] = self.include_tx_power
        if self.discoverable:
            properties["Discoverable"] = True
        return {"org.bluez.LEAdvertisement1": properties}

    def get_path(self):
        return dbus.ObjectPath(self.path)


# ---------------------------------------------------------------------------
# Pairing agent (NoInputNoOutput — just works pairing)
# ---------------------------------------------------------------------------
class Agent(dbus.service.Object):
    @dbus.service.method(
        "org.bluez.Agent1", in_signature="", out_signature=""
    )
    def Release(self):
        print("Agent.Release")

    @dbus.service.method(
        "org.bluez.Agent1", in_signature="os", out_signature=""
    )
    def AuthorizeService(self, device, uuid):
        print(f"Agent.AuthorizeService({device}, {uuid})")
        return

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        print(f"Agent.RequestPinCode({device})")
        return "0000"

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        # Generate a random 6-digit passkey that the user will see and
        # confirm on the phone. DisplayOnly capability means we can show
        # a passkey but cannot accept input.
        import random
        passkey = random.randint(0, 999999)
        print(f"Agent.RequestPasskey({device}) → {passkey:06d}")
        print(f"  >>> If prompted on phone, enter: {passkey:06d}")
        return dbus.UInt32(passkey)

    @dbus.service.method("org.bluez.Agent1", in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        print(f"Agent.DisplayPasskey({device}, {passkey:06d}, entered={entered})")

    @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        print(f"Agent.RequestConfirmation({device}, {passkey:06d})")
        print(f"  >>> Confirm this matches your phone: {passkey:06d}")
        return

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        print(f"Agent.RequestAuthorization({device})")
        return

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Cancel(self):
        print("Agent.Cancel")


# ---------------------------------------------------------------------------
# Linux evdev -> HID keycode translation
# ---------------------------------------------------------------------------
# Linux evdev KEY_* code -> HID usage. Ranges cover keyboard + some extras.
def build_keymap():
    # KEY_RESERVED(0)==HID 0; KEY_ESC(1)==HID 0x29; ... linear until KEY_KPDOT
    m = {}
    # Canonical Linux evdev KEY_* -> HID Usage mapping.
    # This matches the table in the kernel (drivers/hid/hid-input.c) and the
    # "Universal Serial Bus HID Usage Tables" (HID Usage 0x07 keyboard page).
    linux_to_hid = {
        # --- Top number row + ESC / BACKSPACE / TAB ---
        1: 0x29,   # ECS
        2: 0x1E,   # 1
        3: 0x1F,   # 2
        4: 0x20,   # 3
        5: 0x21,   # 4
        6: 0x22,   # 5
        7: 0x23,   # 6
        8: 0x24,   # 7
        9: 0x25,   # 8
        10: 0x26,  # 9
        11: 0x27,  # 0
        12: 0x2D,  # -
        13: 0x2E,  # =
        14: 0x2A,  # BACKSPACE
        15: 0x2B,  # TAB
        # --- Top letter row: QWERTYUIOP[]+ENTER ---
        16: 0x14,  # q
        17: 0x1A,  # w
        18: 0x08,  # e
        19: 0x15,  # r
        20: 0x17,  # t
        21: 0x1C,  # y
        22: 0x18,  # u
        23: 0x0C,  # i
        24: 0x12,  # o
        25: 0x13,  # p
        26: 0x2F,  # [
        27: 0x30,  # ]
        28: 0x28,  # ENTER
        # --- ASDFGHJKL;'` ---
        30: 0x04,  # a
        31: 0x16,  # s
        32: 0x07,  # d
        33: 0x09,  # f
        34: 0x0A,  # g
        35: 0x0B,  # h
        36: 0x0D,  # j
        37: 0x0E,  # k
        38: 0x0F,  # l
        39: 0x33,  # ;
        40: 0x34,  # '
        41: 0x35,  # `
        # --- Bottom letter row: ZXCVBNM,./ ---
        43: 0x64,  # non-US backslash
        44: 0x1D,  # z
        45: 0x1B,  # x
        46: 0x06,  # c
        47: 0x19,  # v
        48: 0x05,  # b
        49: 0x11,  # n
        50: 0x10,  # m
        51: 0x36,  # ,
        52: 0x37,  # .
        53: 0x38,  # /
        54: 0xE5,  # RIGHTSHIFT (modifier, bit5 instead via MODIFIER_BITS)
        # --- SPACE / CAPSLOCK / Function keys ---
        57: 0x2C,  # SPACE
        58: 0x39,  # CAPSLOCK
        59: 0x3A,  # F1
        60: 0x3B,  # F2
        61: 0x3C,  # F3
        62: 0x3D,  # F4
        63: 0x3E,  # F5
        64: 0x3F,  # F6
        65: 0x40,  # F7
        66: 0x41,  # F8
        67: 0x42,  # F9
        68: 0x43,  # F10
        87: 0x44,  # F11
        88: 0x45,  # F12
        # --- Navigation ---
        110: 0x49,  # INSERT (Keyboard Insert)
        102: 0x4A,  # HOME (Keyboard Home)
        104: 0x4B,  # PAGEUP (Keyboard PageUp)
        111: 0x4C,  # DELETE (Keyboard Delete Forward)
        107: 0x4D,  # END (Keyboard End)
        109: 0x4E,  # PAGEDOWN (Keyboard PageDown)
        106: 0x4F,  # RIGHT (Keyboard RightArrow)
        105: 0x50,  # LEFT (Keyboard LeftArrow)
        108: 0x51,  # DOWN (Keyboard DownArrow)
        103: 0x52,  # UP (Keyboard UpArrow)
        # --- Numpad ---
        69: 0x53,   # NUMLOCK
        98: 0x54,   # KP_SLASH
        55: 0x55,   # KP_MULTIPLY (*)
        74: 0x56,   # KP_MINUS (-)
        78: 0x57,   # KP_PLUS (+)
        96: 0x58,   # KP_ENTER
        79: 0x59,   # KP_1
        80: 0x5A,   # KP_2
        81: 0x5B,   # KP_3
        75: 0x5C,   # KP_4
        76: 0x5D,   # KP_5
        77: 0x5E,   # KP_6
        71: 0x5F,   # KP_7
        72: 0x60,   # KP_8
        73: 0x61,   # KP_9
        82: 0x62,   # KP_0
        83: 0x63,   # KP_DOT
        # Modifiers handled in MODIFIER_BITS (29,42,56,97,54,100,125,126)
    }
    return linux_to_hid


# Modifier keys: evdev code -> bit position in modifier byte
MODIFIER_BITS = {
    29: 0,   # KEY_LEFTCTRL  -> LCtrl (bit0)
    42: 1,   # KEY_LEFTSHIFT -> LShift (bit1)
    56: 2,   # KEY_LEFTALT   -> LAlt   (bit2)
    125: 3,  # KEY_LEFTMETA  -> LGUI   (bit3)
    97: 4,   # KEY_RIGHTCTRL -> RCtrl  (bit4)
    54: 5,   # KEY_RIGHTSHIFT-> RShift (bit5)
    100: 6,  # KEY_RIGHTALT  -> RAlt   (bit6)
    126: 7,  # KEY_RIGHTMETA -> RGUI   (bit7)
}


# ---------------------------------------------------------------------------
# Keyboard input sender with reconnection support
# ---------------------------------------------------------------------------
class HIDKeyboardSender:
    """Reads evdev from the physical keyboard and pushes HID reports.

    Implements notification throttling + coalescing to prevent BlueZ from
    silently dropping ATT notifications under TX backpressure. BlueZ 5.82's
    notification path uses fire-and-forget writev() — on EAGAIN the packet
    is destroyed with no retry. By enforcing a minimum interval between
    reports and coalescing pending ones, we stay within the BLE link's
    capacity.
    """

    EVIOCGRAB = 0x40044590

    # struct input_event on Linux (64-bit): struct timeval (2 x long = 16 B) +
    # __u16 type (2 B) + __u16 code (2 B) + __s32 value (4 B) = 24 bytes.
    EV_FMT = "llHHi"
    EV_SIZE = struct.calcsize(EV_FMT)

    # Minimum milliseconds between sent notifications. BLE LE connection
    # intervals are typically 15-30ms on iOS/Android; 30ms gives the L2CAP
    # socket time to drain between bursts.
    NOTIFY_MIN_INTERVAL_MS = 30

    def __init__(self, input_path, report_char, log=print, debug=False):
        self.input_path = input_path
        self.report_char = report_char
        self.log = log
        self.debug = debug
        self.pressed = set()  # HID usages currently held
        self.modifiers = 0x00
        self.fd = None
        self.linux_to_hid = build_keymap()
        # Throttling state
        self._last_send_ms = 0     # monotonic time of last notification sent
        self._pending_report = None  # report queued for next window

    def open(self):
        # Passive read — no EVIOCGRAB. The kernel multicasts input events to
        # all evdev clients, so X keeps getting keystrokes too. This prevents
        # lock-screen lockouts when the BLE service is running.
        self.fd = os.open(self.input_path, os.O_RDONLY | os.O_NONBLOCK)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def reset_state(self):
        """Clear all held keys and modifiers — call on disconnect."""
        self.pressed.clear()
        self.modifiers = 0x00
        self._last_report = None
        self._pending_report = None

    def _make_report(self):
        """Build a standard 8-byte boot keyboard report."""
        keycodes = sorted(self.pressed)[:6]
        while len(keycodes) < 6:
            keycodes.append(0)
        return bytes([self.modifiers, 0x00]) + bytes(keycodes)

    def _now_ms(self):
        return time.monotonic_ns() // 1_000_000

    def process_events(self):
        """Non-blocking read of evdev events, returns True if any processed.

        Handles short reads on non-blocking fds by buffering partial data and
        only parsing complete 24-byte input_event structs.
        """
        moved = False
        if not hasattr(self, "_buf"):
            self._buf = b""
        while True:
            try:
                data = os.read(self.fd, 4096)
            except BlockingIOError:
                break
            except OSError as e:
                if e.errno == errno.EAGAIN:
                    break
                self.log(f"input read error: {e}")
                self.close()
                return False
            if not data:
                break
            self._buf += data
            # Parse as many complete 24-byte events as available
            while len(self._buf) >= self.EV_SIZE:
                chunk, self._buf = self._buf[:self.EV_SIZE], self._buf[self.EV_SIZE:]
                sec, usec, type_, code, value = struct.unpack(self.EV_FMT, chunk)
                if type_ == 1:  # EV_KEY
                    self._handle_key(code, value)
                    moved = True
        # After processing all events, flush any pending throttled report.
        self._flush_pending()
        return moved

    def _handle_key(self, name_code, value):
        # name_code is the evdev KEY_ code
        # 'value' is 1 = press, 0 = release, 2 = auto-repeat
        if name_code in MODIFIER_BITS:
            bit = MODIFIER_BITS[name_code]
            if value == 1:
                self.modifiers |= (1 << bit)
            elif value == 0:
                self.modifiers &= ~(1 << bit)
            # Auto-repeat ignores modifiers (they were already sent)
        else:
            hid = self.linux_to_hid.get(name_code)
            if hid is None:
                return
            if value == 1:
                self.pressed.add(hid)
            elif value == 0:
                self.pressed.discard(hid)
            # On auto-repeat (value==2), pressed set is unchanged —
            # but we still queue a report so the central re-renders the key.
        # value==2 (auto-repeat): report is identical to last.  Normally dedup
        # would suppress it, but we MUST send it for repeat to work.  Pass
        # force=True to bypass the dedup check while still respecting throttle.
        force = (value == 2)
        self._send_report(force=force)

    def _send_report(self, force=False):
        """Queue a report for sending with throttling + coalescing.

        - Dedup: skip if identical to last sent (unless force=True for repeats).
        - Throttle: enforce NOTIFY_MIN_INTERVAL_MS between actual notifications.
        - Coalesce: if a report is pending from a previous throttle window,
          replace it with the newest — only the latest state matters.
        """
        report = self._make_report()
        last = getattr(self, "_last_report", None)
        if not force and report == last:
            return
        self._last_report = report
        if self.debug:
            self.log(f"  [HID report] {report.hex()}")
        # Check if we're outside the throttle window
        now = self._now_ms()
        elapsed = now - self._last_send_ms
        if elapsed >= self.NOTIFY_MIN_INTERVAL_MS:
            # Window is open — send immediately
            self._last_send_ms = now
            self._pending_report = None
            try:
                self.report_char.set_hid_report(report)
            except Exception as e:
                self.log(f"send failed (device gone?): {e}")
        else:
            # Window is closed — coalesce: keep only the newest pending report
            if force and self._pending_report is None:
                if self.debug:
                    self.log(f"  [throttle] queuing forced report, {self.NOTIFY_MIN_INTERVAL_MS - elapsed}ms until send")
            self._pending_report = report

    def _flush_pending(self):
        """Send any throttled/coalesced report whose window has opened.

        Called after process_events() drains the evdev queue and on each
        GLib poll tick (io_readable) even when no input arrived.
        """
        if self._pending_report is None:
            return
        now = self._now_ms()
        if now - self._last_send_ms >= self.NOTIFY_MIN_INTERVAL_MS:
            report = self._pending_report
            self._pending_report = None
            self._last_send_ms = now
            if self.debug:
                self.log(f"  [flush] {report.hex()}")
            try:
                self.report_char.set_hid_report(report)
            except Exception as e:
                self.log(f"flush send failed (device gone?): {e}")


# ---------------------------------------------------------------------------
# Main wiring
# ---------------------------------------------------------------------------
def print_section(txt):
    print("\n" + "=" * 60)
    print("  " + txt)
    print("=" * 60)


def main():
    ap = argparse.ArgumentParser(description="BLE HID keyboard adapter")
    ap.add_argument("--input", default="/dev/input/event0",
                    help="evdev keyboard input device (default /dev/input/event0)")
    ap.add_argument("--alias", default="crackedbook-kbd",
                    help="BLE advertised name (default crackedbook-kbd)")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    bus = dbus.SystemBus()
    mainloop = GLib.MainLoop()

    # --- Adapter discovery ---
    try:
        adapter_obj = bus.get_object("org.bluez", "/org/bluez/hci0")
        props = dbus.Interface(adapter_obj, "org.freedesktop.DBus.Properties")
        adapter_addr = str(props.Get("org.bluez.Adapter1", "Address"))
    except dbus.exceptions.DBusException as e:
        print(f"FATAL: cannot reach Bluetooth adapter /org/bluez/hci0: {e}")
        sys.exit(1)

    print_section("BLE HID Keyboard Adapter starting")
    print(f"  Adapter: {adapter_addr}")
    print(f"  Input device: {args.input}")
    print(f"  Advertised as: {args.alias}")

    def prop_set(name, value):
        props.Set("org.bluez.Adapter1", name, dbus.Boolean(value) if isinstance(value, bool) else value)

    try:
        prop_set("Powered", True)
        prop_set("Discoverable", True)
        prop_set("Pairable", True)
    except dbus.exceptions.DBusException as e:
        print(f"WARN: could not power/enable adapter: {e}")

    # --- Open keyboard input device ---
    try:
        keyboard = HIDKeyboardSender(args.input, None, print, debug=args.debug)
        keyboard.open()
    except OSError as e:
        print(f"FATAL: cannot open input device {args.input}: {e}")
        print("  Run as root or `sudo usermod -aG input $USER` then re-login.")
        sys.exit(1)

    # --- Build GATT application tree ---
    # NOTE: BlueZ already exports the GAP (0x1800) and GATT (0x1801) services
    # internally. Registering duplicates of either causes
    #   "org.bluez.Error.Failed: Failed to create entry in database"
    # so we register ONLY the HID service. The device name is set via the
    # adapter Alias, and HID works fine without a custom appearance.
    app = Application(bus)

    # HID service
    hid_svc = Service(bus, 2, BLE_UUID_HID, True)
    hid_info = HIDInformationCharacteristic(bus, 0, hid_svc)
    report_map = ReportMapCharacteristic(bus, 1, hid_svc)
    protocol_mode = ProtocolModeCharacteristic(bus, 2, hid_svc)
    report = ReportCharacteristic(bus, 3, hid_svc)
    hid_control_point = HIDControlPointCharacteristic(bus, 4, hid_svc)
    pnp = PNPCharacteristic(bus, 5, hid_svc)
    hid_svc.add_characteristic(hid_info)
    hid_svc.add_characteristic(report_map)
    hid_svc.add_characteristic(protocol_mode)
    hid_svc.add_characteristic(report)
    hid_svc.add_characteristic(hid_control_point)
    hid_svc.add_characteristic(pnp)
    report.add_descriptor(report.report_ref)
    # NOTE: BlueZ automatically manages the CCCD for characteristics with the
    # 'notify' flag. We do NOT add a custom CCCD descriptor — doing so creates
    # a duplicate that conflicts with BlueZ's internal CCCD handling.
    app.add_service(hid_svc)

    keyboard.report_char = report
    print(f"  GATT application registered; report char path={report.get_path()}")

    # --- Register GATT application ---
    gatt_manager = dbus.Interface(
        bus.get_object("org.bluez", "/org/bluez/hci0"), "org.bluez.GattManager1"
    )
    try:
        gatt_manager.RegisterApplication(app.get_path(), {}, reply_handler=app_registered,
                                         error_handler=lambda e: register_err(e))
    except dbus.exceptions.DBusException as e:
        print(f"RegisterApplication failed: {e}")
        sys.exit(1)

    # GATT DB registered. Advertisement registration is done after a
    # brief yield to let RegisterApplication's async callback complete,
    # ensuring the GATT DB is fully exported before advertising starts.
    import time as _time
    _time.sleep(0.5)

    # --- Register advertisement ---
    adv = Advertisement(bus, 0, "peripheral", args.alias)
    adv_manager = dbus.Interface(
        bus.get_object("org.bluez", "/org/bluez/hci0"), "org.bluez.LEAdvertisingManager1"
    )
    try:
        adv_manager.RegisterAdvertisement(adv.get_path(), {}, reply_handler=adv_registered,
                                          error_handler=lambda e: print(f"adv reg err: {e}"))
    except dbus.exceptions.DBusException as e:
        print(f"RegisterAdvertisement failed: {e}")

    agent = Agent(bus, AGENT_PATH)
    agent_manager = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"), "org.bluez.AgentManager1")
    agent_manager.RegisterAgent(AGENT_PATH, "NoInputNoOutput")
    agent_manager.RequestDefaultAgent(AGENT_PATH)

    # --- Periodic input poll (non-blocking, drives the GLib main loop) ---
    def io_readable():
        keyboard.process_events()
        # Always flush pending throttled reports, even if no input arrived.
        # The 10ms tick ensures a queued report waits at most one tick past
        # its throttle window before being sent.
        keyboard._flush_pending()
        return True

    # Also handle connection state for reconnection logging
    def properties_changed(interface, changed, invalidated, path):
        if interface == "org.bluez.Device1":
            if "Connected" in changed:
                connected = bool(changed["Connected"])
                print(f"  Device {path} connected={connected}")
                if not connected:
                    keyboard.reset_state()

    bus.add_signal_receiver(
        properties_changed,
        dbus_interface="org.freedesktop.DBus.Properties",
        signal_name="PropertiesChanged",
        path_keyword="path",
    )

    # Poll input every 10 ms
    GLib.timeout_add(10, io_readable)

    print_section("Ready — advertising as a BLE keyboard")
    print("  Apple devices: go to Settings > Bluetooth and tap the device.")
    print("  Ctrl-C to stop.")

    signal.signal(signal.SIGINT, lambda *_: mainloop.quit())
    signal.signal(signal.SIGTERM, lambda *_: mainloop.quit())
    try:
        mainloop.run()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            adv_manager.UnregisterAdvertisement(adv.get_path())
        except Exception:
            pass
        try:
            gatt_manager.UnregisterApplication(app.get_path())
        except Exception:
            pass
        keyboard.close()
        print("\nStopped.")


def app_registered():
    print("  GattApplication registered.")


def register_err(e):
    print(f"  RegisterApplication error: {e}")


def adv_registered():
    print("  Advertisement registered.")


if __name__ == "__main__":
    main()
