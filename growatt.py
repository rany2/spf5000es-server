#!/usr/bin/env python3

"""Allows to read and write data from Growatt inverters using Modbus RTU over RS485."""

import base64
import binascii
import configparser
import hashlib
import hmac
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer as HTTPServer
from json import dumps as json_dumps
from queue import Queue
from threading import Event, Lock, Thread
from time import sleep, time
from typing import Dict, List, Optional, Tuple, Union, override
from urllib.parse import parse_qs

from pymodbus.client import ModbusSerialClient as ModbusClient
from pymodbus.exceptions import ModbusException


def str2bool(value: Union[str, bool, int]) -> bool:
    """Converts a string to a boolean value."""
    if isinstance(value, (bool, int)):
        return bool(value)

    if value.lower() in ("yes", "true", "t", "1"):
        return True

    if value.lower() in ("no", "false", "f", "0"):
        return False

    raise ValueError("Boolean value expected")


def str2bool2int(value: Union[str, bool, int]) -> int:
    """Converts a string to a boolean value and then to an integer."""
    return int(str2bool(value))


## Register Types ##
class RegType(Enum):
    """Enum for register types."""

    UINT = 0
    INT = 1
    CHAR = 2


## Input Registers ##
SystemStatusR = {
    0: "Standby",
    1: "PV&Grid Supporting Loads",
    2: "Battery Discharging",
    3: "Fault",
    4: "Flash",
    5: "PV Charging",
    6: "Grid Charging",
    7: "PV&Grid Charging",
    8: "PV&Grid Charging+Grid Bypass",
    9: "PV Charging+Grid Bypass",
    10: "Grid Charging+Grid Bypass",
    11: "Grid Bypass",
    12: "PV Charging+Loads Supporting",
    13: "Export to Grid",
}
InputRegisters = {
    # Register: (Start, Length, Type, PostProcess)
    # fmt: off
    "SystemStatus": (0, 1, RegType.UINT, lambda x: SystemStatusR[x]),
    "PV1Volt": (1, 1, RegType.UINT, lambda x: x / 10),
    "PV2Volt": (2, 1, RegType.UINT, lambda x: x / 10),
    "PV1Watt": (3, 2, RegType.UINT, lambda x: x / 10),
    "PV2Watt": (5, 2, RegType.UINT, lambda x: x / 10),
    "PV1Amps": (7, 1, RegType.UINT, lambda x: x / 10),
    "PV2Amps": (8, 1, RegType.UINT, lambda x: x / 10),
    "OutputWatt": (9, 2, RegType.UINT, lambda x: x / 10),
    "OutputVA": (11, 2, RegType.UINT, lambda x: x / 10),
    "ACChrWatt": (13, 2, RegType.UINT, lambda x: x / 10),
    "ACChrVA": (15, 2, RegType.UINT, lambda x: x / 10),
    "BatteryVolt": (17, 1, RegType.UINT, lambda x: x / 100),
    "BatterySOC": (18, 1, RegType.UINT, int),
    "BusVolt": (19, 1, RegType.UINT, lambda x: x / 10),
    "GridVolt": (20, 1, RegType.UINT, lambda x: x / 10),
    "LineFreq": (21, 1, RegType.UINT, lambda x: x / 100),
    "OutputACVolt": (22, 1, RegType.UINT, lambda x: x / 10),
    "OutputACFreq": (23, 1, RegType.UINT, lambda x: x / 100),
    "OutputDCVolt": (24, 1, RegType.UINT, lambda x: x / 10),
    "InvTempC": (25, 1, RegType.INT, lambda x: x / 10),
    "DCDCTempC": (26, 1, RegType.INT, lambda x: x / 10),
    "LoadPercent": (27, 1, RegType.UINT, lambda x: x / 10),
    "BatteryPortVolt": (28, 1, RegType.UINT, lambda x: x / 100),
    "BatteryBusVolt": (29, 1, RegType.UINT, lambda x: x / 100),
    "WorkTimeTotalSeconds": (30, 2, RegType.UINT, lambda x: x / 2),
    "Buck1TempC": (32, 1, RegType.INT, lambda x: x / 10),
    "Buck2TempC": (33, 1, RegType.INT, lambda x: x / 10),
    "OutputAmps": (34, 1, RegType.UINT, lambda x: x / 10),
    "InvAmps": (35, 1, RegType.UINT, lambda x: x / 10),
    "ACInputWatt": (36, 2, RegType.INT, lambda x: x / 10), # > 0: From Grid, < 0: To Grid
    "ACInputVA": (38, 2, RegType.UINT, lambda x: x / 10),
    "FaultBit": (40, 1, RegType.UINT, int),
    "WarningBit": (41, 1, RegType.UINT, int),
    "WarningBitHigh": (42, 1, RegType.UINT, int),
    "WarningValue": (43, 1, RegType.UINT, int),
    "DeviceTypeCode": (44, 1, RegType.UINT, int),
    "ExportToGridTodaykWh": (45, 1, RegType.UINT, lambda x: x / 10),
    "ExportToGridTotalkWh": (46, 2, RegType.UINT, lambda x: x / 10),
    "PV1EnergyTodaykWh": (48, 2, RegType.UINT, lambda x: x / 10),
    "PV1EnergyTotalkWh": (50, 2, RegType.UINT, lambda x: x / 10),
    "PV2EnergyTodaykWh": (52, 2, RegType.UINT, lambda x: x / 10),
    "PV2EnergyTotalkWh": (54, 2, RegType.UINT, lambda x: x / 10),
    "ACChargeEnergyTodaykWh": (56, 2, RegType.UINT, lambda x: x / 10),
    "ACChargeEnergyTotalkWh": (58, 2, RegType.UINT, lambda x: x / 10),
    "BatteryDischargeEnergyTodaykWh": (60, 2, RegType.UINT, lambda x: x / 10),
    "BatteryDischargeEnergyTotalkWh": (62, 2, RegType.UINT, lambda x: x / 10),
    "ACDischargeEnergyTodaykWh": (64, 2, RegType.UINT, lambda x: x / 10),
    "ACDischargeEnergyTotalkWh": (66, 2, RegType.UINT, lambda x: x / 10),
    "ACChargeBatteryAmps": (68, 1, RegType.UINT, lambda x: x / 10),
    "ACDischargeWatt": (69, 2, RegType.UINT, lambda x: x / 10),
    "ACDischargeVA": (71, 2, RegType.UINT, lambda x: x / 10),
    "BatteryDischargeWatt": (73, 2, RegType.UINT, lambda x: x / 10),
    "BatteryDischargeVA": (75, 2, RegType.UINT, lambda x: x / 10),
    "BatteryWatt": (77, 2, RegType.INT, lambda x: x / 10), # > 0: Discharge, < 0: Charge
    "MpptFanSpeedPercent": (81, 1, RegType.UINT, int),
    "InvFanSpeedPercent": (82, 1, RegType.UINT, int),
    "TotalChargeAmps": (83, 1, RegType.UINT, lambda x: x / 10),
    "TotalDischargeAmps": (84, 1, RegType.UINT, lambda x: x / 10),
    "OPDischargeEnergyTodaykWh": (85, 2, RegType.UINT, lambda x: x / 10),
    "OPDischargeEnergyTotalkWh": (87, 2, RegType.UINT, lambda x: x / 10),
    "ParaSystemChargeAmps": (90, 1, RegType.UINT, lambda x: x / 10),
    # fmt: on
}

## Holding Registers ##
OutputConfigR = {0: "SBU", 1: "SOL", 2: "UTI", 3: "SUB"}
OutputConfigW = {v: k for k, v in OutputConfigR.items()}
ChargeConfigR = {0: "PV First", 1: "PV&UTI", 2: "PV Only"}
ChargeConfigW = {v: k for k, v in ChargeConfigR.items()}
PVModelR = {0: "Independent", 1: "Parallel"}
PVModelW = {v: k for k, v in PVModelR.items()}
ACInModelR = {0: "APL", 1: "UPS", 2: "GEN"}
ACInModelW = {v: k for k, v in ACInModelR.items()}
OutputVoltTypeR = {
    0: "208VAC",
    1: "230VAC",
    2: "240VAC",
    3: "220VAC",
    4: "100VAC",
    5: "110VAC",
    6: "120VAC",
}
OutputVoltTypeW = {v: k for k, v in OutputVoltTypeR.items()}
OutputFreqTypeR = {0: "50Hz", 1: "60Hz"}
OutputFreqTypeW = {v: k for k, v in OutputFreqTypeR.items()}
OverLoadRestartR = {0: "Yes", 1: "No", 2: "Switch to UTI"}
OverLoadRestartW = {v: k for k, v in OverLoadRestartR.items()}
OverTempRestartR = {0: True, 1: False}
OverTempRestartW = {v: k for k, v in OverTempRestartR.items()}
BatteryTypeR = {0: "AGM", 1: "FLD", 2: "USE", 3: "Lithium", 4: "USE2"}
BatteryTypeW = {v: k for k, v in BatteryTypeR.items()}
AgingModeR = {0: "Normal", 1: "Aging"}
AgingModeW = {v: k for k, v in AgingModeR.items()}
SafetyTypeR = {1: "Standard", 2: "ETL", 3: "AS4777", 4: "CQC", 5: "VDE4105"}
SafetyTypeW = {v: k for k, v in SafetyTypeR.items()}
OnOffR = {0x0000: "Output enable", 0x0100: "Output disable"}
HoldingAndWriteRegisters = {
    # Register: (Start, Length, Type, ReadPostProcess, WritePreProcess (None if not writeable))
    # fmt: off
    "OnOff": (0, 1, RegType.UINT, lambda x: OnOffR[x], None),
    "OutputConfig": (1, 1, RegType.UINT, lambda x: OutputConfigR[x], lambda x: OutputConfigW[x]),
    "ChargeConfig": (2, 1, RegType.UINT, lambda x: ChargeConfigR[x], lambda x: ChargeConfigW[x]),
    "UtiOutStart": (3, 1, RegType.UINT, int, int),     # 0-23
    "UtiOutEnd": (4, 1, RegType.UINT, int, int),       # 0-23
    "UtiChargeStart": (5, 1, RegType.UINT, int, int),  # 0-23
    "UtiChargeEnd": (6, 1, RegType.UINT, int, int),    # 0-23
    "PVModel": (7, 1, RegType.UINT, lambda x: PVModelR[x], lambda x: PVModelW[x]),
    "ACInModel": (8, 1, RegType.UINT, lambda x: ACInModelR[x], lambda x: ACInModelW[x]),
    "FWVersion": (9, 3, RegType.CHAR, str, None),
    "FWVersion2": (12, 3, RegType.CHAR, str, None),
    "LCDLanguage": (15, 1, RegType.UINT, int, int),
    "GridV_Adj": (16, 1, RegType.UINT, int, None),
    "InvV_Adj": (17, 1, RegType.UINT, int, None),
    "OutputVoltType": (18, 1, RegType.UINT,
                       lambda x: OutputVoltTypeR[x],
                       lambda x: OutputVoltTypeW[x]),
    "OutputFreqType": (19, 1, RegType.UINT,
                       lambda x: OutputFreqTypeR[x],
                       lambda x: OutputFreqTypeW[x]),
    "OverLoadRestart": (20, 1, RegType.UINT,
                        lambda x: OverLoadRestartR[x],
                        lambda x: OverLoadRestartW[x]),
    "OverTempRestart": (21, 1, RegType.UINT,
                        lambda x: OverTempRestartR[x],
                        lambda x: OverTempRestartW[str2bool(x)]),
    "BuzzerEnable": (22, 1, RegType.UINT, bool, str2bool2int),
    "SerialNumber": (23, 5, RegType.CHAR, str, str),
    "MoudleH": (28, 1, RegType.UINT, int, int),
    "MoudleL": (29, 1, RegType.UINT, int, int),
    "ComAddress": (30, 1, RegType.UINT, int, int),
    "FlashStart": (31, 1, RegType.UINT, int, int),
    "ResetUserInfo": (32, 1, RegType.UINT, int, int),
    "ResetToFactory": (33, 1, RegType.UINT, int, int),
    "MaxChargeAmps": (34, 1, RegType.UINT, int, int),
    "BulkChargeVolt": (35, 1, RegType.UINT, lambda x: x / 10, lambda x: int(x) * 10),
    "FloatChargeVolt": (36, 1, RegType.UINT, lambda x: x / 10, lambda x: int(x) * 10),
    "BatLowtoUti": (37, 1, RegType.UINT, lambda x: x / 10, lambda x: int(x) * 10),
    "ACChargeAmps": (38, 1, RegType.UINT, int, int),
    "BatteryType": (39, 1, RegType.UINT, lambda x: BatteryTypeR[x], lambda x: BatteryTypeW[x]),
    "AgingMode": (40, 1, RegType.UINT, lambda x: AgingModeR[x], lambda x: AgingModeW[x]),
    "FunctionMask": (41, 1, RegType.UINT, int, int),
    "SafetyType": (42, 1, RegType.UINT, lambda x: SafetyTypeR[x], lambda x: SafetyTypeW[x]),
    "DTC": (43, 1, RegType.UINT, int, None),
    "SysYear": (45, 1, RegType.UINT, int, int),
    "SysMonth": (46, 1, RegType.UINT, int, int),
    "SysDay": (47, 1, RegType.UINT, int, int),
    "SysHour": (48, 1, RegType.UINT, int, int),
    "SysMin": (49, 1, RegType.UINT, int, int),
    "SysSec": (50, 1, RegType.UINT, int, int),
    #"uwAcVoltHighL": (51, 1, RegType.UINT, int, None),
    #"uwAcVoltLowL": (52, 1, RegType.UINT, int, None),
    #"uwAcFreqHighL": (53, 1, RegType.UINT, int, None),
    #"uwAcFreqLowL": (54, 1, RegType.UINT, int, None),
    #"ManufacturerInfo": (59, 8, RegType.CHAR, str, None),
    #"FWBuildNo4": (67, 1, RegType.UINT, int, None),
    #"FWBuildNo3": (68, 1, RegType.UINT, int, None),
    #"FWBuildNo2": (69, 1, RegType.UINT, int, None),
    #"FWBuildNo1": (70, 1, RegType.UINT, int, None),
    "SysWeekly": (72, 1, RegType.UINT, int, int),
    "ModbusVersion": (73, 1, RegType.UINT, lambda x: x / 100, None),
    "SCC_ComMode": (75, 1, RegType.UINT, int, None),
    "RateWatt": (76, 2, RegType.UINT, lambda x: x / 10, None),
    "RateVA": (78, 2, RegType.UINT, lambda x: x / 10, None),
    "ComboardVer": (80, 1, RegType.UINT, int, None),
    "uwBatPieceNum": (81, 1, RegType.UINT, int, int),
    "wBatLowCutOff": (82, 1, RegType.UINT, lambda x: x / 10, None),
    #"NomGridVolt": (84, 1, RegType.UINT, int, None),
    #"NomGridFreq": (85, 1, RegType.UINT, int, None),
    #"NomBatVolt": (86, 1, RegType.UINT, int, None),
    #"NomPvAmps": (87, 1, RegType.UINT, int, None),
    #"NomAcChgAmps": (88, 1, RegType.UINT, int, None),
    #"NomOpVolt": (89, 1, RegType.UINT, int, None),
    #"NomOpFreq": (90, 1, RegType.UINT, int, None),
    #"NomOpPow": (91, 1, RegType.UINT, int, None),
    "uwAC2BatVolt": (95, 1, RegType.UINT, lambda x: x / 10, lambda x: int(x) * 10),
    "BypEnable": (96, 1, RegType.UINT, bool, str2bool2int),
    "PowSavingEnable": (97, 1, RegType.UINT, bool, str2bool2int),
    "SpowBalEnable": (98, 1, RegType.UINT, bool, str2bool2int),
    "ClrEnergyToday": (99, 1, RegType.UINT, bool, str2bool2int),
    "ClrEnergyAll": (100, 1, RegType.UINT, bool, str2bool2int),
    "BurnInTestEnable": (101, 1, RegType.UINT, bool, str2bool2int),
    "ManualStartEnable": (102, 1, RegType.UINT, bool, str2bool2int),
    "SciLossChkEnable": (103, 1, RegType.UINT, bool, str2bool2int),
    "BlightEnable": (104, 1, RegType.UINT, bool, str2bool2int),
    "ParaMaxChgAmps": (105, 1, RegType.UINT, int, None),
    "LiProtocolType": (106, 1, RegType.UINT, int, int),
    "AudioAlarmEnable": (107, 1, RegType.UINT, bool, str2bool2int),
    "uwEqEnable": (108, 1, RegType.UINT, bool, str2bool2int),
    "uwEqChgVolt": (109, 1, RegType.UINT, int, int),
    "uwEqTime": (110, 1, RegType.UINT, int, int),
    "uwEqTimeOut": (111, 1, RegType.UINT, int, int),
    "uwEqInterval": (112, 1, RegType.UINT, int, int),
    "uwMaxDisChgAmps": (113, 1, RegType.UINT, int, int),
    "BLVersion2": (162, 1, RegType.UINT, int, None),
    # fmt: on
}


def generate_index_html():
    """Generates the index HTML page."""
    index_html = (
        "<!DOCTYPE html><html lang='en'><head><title>Growatt</title>"
        "<style>body{font-family:Arial,Helvetica,sans-serif;}</style>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "</head><body><h1>Growatt</h1><h2>View Data:</h2><ul>"
        "<li><a href='status' target='_blank'>System Status</a>"
        "<li><a href='config' target='_blank'>System Configuration</a></ul>"
        "<h2>Modify Configuration:</h2><form action='config' method='GET'>"
        "<input type='text' name='key' placeholder='Key'>"
        "<input type='text' name='value' placeholder='Value'>"
        "<input type='submit' value='Submit'>"
        "<input type='hidden' name='_method' value='PUT'></form>"
        "<p>Writeable keys:</p><ul>"
    )
    for key, item in HoldingAndWriteRegisters.items():
        if not item[4]:  # not writeable
            continue
        index_html += f"<li>{key}</li>"
    index_html += "</ul></body></html>"
    index_html = index_html.encode("utf-8")
    return index_html


## HTTP Server ##
INDEX_HTML = generate_index_html()
CONTENT_TYPE_JSON = "application/json; charset=utf-8"
CONTENT_TYPE_TEXT = "text/plain; charset=utf-8"
CONTENT_TYPE_HTML = "text/html; charset=utf-8"


class GrowattModbusClient:
    """Modbus RTU client with lock that adheares to Growatt specifications in terms
    of waiting time between requests when necessary.

    pymodbus is not thread-safe, so we need to use a lock to prevent concurrent access.
    Source: https://pymodbus.readthedocs.io/en/v3.7.0/source/client.html"""

    MIN_WAIT_TIME_BETWEEN_CMDS = 1  # seconds

    def __init__(self, port: str):
        """
        Args:
            port (str): The serial port to use (e.g. "/dev/ttyUSB0").
        """

        self.client = ModbusClient(
            framer="rtu",
            port=port,
            baudrate=9600,
            stopbits=1,
            bytesize=8,
            parity="N",
        )
        self.lock = Lock()

    @staticmethod
    def lock_wrapper(enforce_wait_time: bool):
        """Decorator factory to lock the function call.

        Args:
            enforce_wait_time (bool): Whether to enforce the minimum wait time.
        """

        def _lock_wrapper(func):
            """Decorator to lock the function call."""

            def wrapper(self: "GrowattModbusClient", *args, **kwargs):
                """Wrapper function to lock the function call."""

                def wait_and_release():
                    """Release the lock after the minimum wait time."""

                    # Wait for the minimum per Growatt Modbus documentation
                    sleep(self.MIN_WAIT_TIME_BETWEEN_CMDS)

                    # Release the lock after waiting for the minimum time
                    self.lock.release()

                try:
                    # Acquire the lock before calling the function
                    self.lock.acquire()

                    # Call the function with the lock acquired
                    return func(self, *args, **kwargs)
                finally:
                    if enforce_wait_time:
                        # Release the lock after the function call
                        # while enforcing the minimum wait time.
                        try:
                            # Start a new thread to wait and release the lock
                            # to prevent blocking the request handler.
                            Thread(target=wait_and_release).start()
                        except Exception as exc:  # pylint: disable=broad-except
                            # Print the exception to stderr.
                            sys.stderr.write(f"[ERROR] {exc}\n")

                            # If we can't start a new thread, just wait and release.
                            # This might happen when we run out of resources.
                            wait_and_release()
                    else:
                        # Release the lock after the function call
                        self.lock.release()

            return wrapper

        return _lock_wrapper

    @lock_wrapper(enforce_wait_time=False)
    def connect(self):
        """Connect to the Modbus server."""
        self.client.connect()

    @lock_wrapper(enforce_wait_time=False)
    def close(self):
        """Close the connection to the Modbus server."""
        self.client.close()

    @lock_wrapper(enforce_wait_time=False)
    def read_input_registers(self, start: int, count: int):
        """Read input registers from the Modbus server."""
        return self.client.read_input_registers(start, count)

    def read_holding_registers_unsafe(self, start: int, count: int = 1):
        """Read holding registers from the Modbus server without
        holding the lock or enforcing the wait time."""
        return self.client.read_holding_registers(start, count)

    @lock_wrapper(enforce_wait_time=False)
    def read_holding_registers(self, start: int, count: int = 1):
        """Read holding registers from the Modbus server."""
        return self.read_holding_registers_unsafe(start, count)

    def write_registers_unsafe(self, address: int, values: int):
        """Write multiple registers to the Modbus server without
        holding the lock or enforcing the wait time.

        This is useful when the operation is time-sensitive and
        we want to ensure that the value is written as soon as
        possible. This is useful for setting the inverter's time."""
        return self.client.write_registers(address, values)

    @lock_wrapper(enforce_wait_time=True)
    def write_registers(self, address: int, values: int):
        """Write multiple registers to the Modbus server."""
        return self.write_registers_unsafe(address, values)


class GrowattInverter:
    """Class to interact with a Growatt inverter using Modbus RTU."""

    def __init__(self, port: str):
        """Initialize the Growatt inverter.

        Args:
            port (str): The serial port to use (e.g. "/dev/ttyUSB0")."""
        self.client = GrowattModbusClient(port)
        self.sync_time_thread = Thread(target=self.sync_time)
        self.sync_time_event = Event()
        self.write_queue = Queue()
        self.write_thread = Thread(target=self.write_registers)
        self.write_event = Event()

    def connect(self):
        """Connect to the Modbus server and start the datetime thread."""
        self.client.connect()
        if not self.sync_time_thread.is_alive():
            self.sync_time_thread.start()
        if not self.write_thread.is_alive():
            self.write_thread.start()

    def close(self):
        """Close the connection to the Modbus server and stop the datetime thread."""
        self.client.close()
        self.sync_time_event.set()
        if self.sync_time_thread.is_alive():
            self.sync_time_thread.join()
        self.write_event.set()
        if self.write_thread.is_alive():
            self.write_thread.join()

    def sync_time(self):
        """Update the inverter's time every 720 seconds."""
        update_interval = 720  # seconds
        while True:
            try:
                self.client.lock.acquire()  # pylint: disable=consider-using-with
                sleep(int(time() + 1) - time())  # Wait until the next exact second
                now = datetime.now()
                values = [
                    now.year,  # SysYear (45)
                    now.month,  # SysMonth (46)
                    now.day,  # SysDay (47)
                    now.hour,  # SysHour (48)
                    now.minute,  # SysMin (49)
                    now.second,  # SysSec (50)
                ]
                self.client.write_registers_unsafe(
                    HoldingAndWriteRegisters["SysYear"][0],  # 45
                    values=values,  # [year, month, day, hour, minute, second]
                )
            except Exception as exc:  # pylint: disable=broad-except
                sys.stderr.write(f"[ERROR] Failed to update time: {exc}\n")
            finally:
                sleep(self.client.MIN_WAIT_TIME_BETWEEN_CMDS)
                self.client.lock.release()

            if self.sync_time_event.wait(timeout=update_interval):
                break

    def write_registers(self):
        """Write registers to the inverter from queue every second."""
        update_interval = 1  # seconds
        while not self.write_event.wait(timeout=update_interval):
            requested = {}
            while not self.write_queue.empty():
                start, values = self.write_queue.get_nowait()
                for i in range(len(values)):
                    requested[start + i] = values[i]

            for start, values in requested.items():
                try:
                    self.client.write_registers(start, values)
                except Exception as exc:
                    sys.stderr.write(f"[ERROR] Failed to write registers: {exc}\n")

    @staticmethod
    def registers_to_bytes(
        registers: List[int], start: int = 0, length: int = 1
    ) -> bytes:
        """Convert multiple registers to a byte string.

        Args:
            registers (List[int]): The registers to convert.
            start (int): The start index of the registers.
            length (int): The number of registers to convert."""
        return b"".join(
            bytes([registers[start + i] >> 8, registers[start + i] & 0xFF])
            for i in range(length)
        )

    @staticmethod
    def bytes_to_registers(data: bytes) -> List[int]:
        """Convert a 16-bit byte string to multiple registers.

        Args:
            data (bytes): The data to convert. Must have an even length
                          which is the case for 16-bit Modbus registers.

        Returns:
            List[int]: The registers"""
        return [data[i] << 8 | data[i + 1] for i in range(0, len(data), 2)]

    @staticmethod
    def combine_registers(
        registers: List[int], start: int = 0, length: int = 1, signed: bool = False
    ) -> int:
        """Combine multiple registers into a single value.

        Args:
            registers (List[int]): The registers to combine.
            start (int): The start index of the registers.
            length (int): The number of registers to combine.
            signed (bool): Whether the value is signed."""
        data = GrowattInverter.registers_to_bytes(registers, start, length)
        return int.from_bytes(data, "big", signed=signed)

    @staticmethod
    def uncombine_registers(value: int, length: int, signed: bool = False) -> List[int]:
        """Uncombine a single value into multiple registers.

        Args:
            value (int): The value to uncombine.
            length (int): The number of registers to uncombine to.
            signed (bool): Whether the value is signed."""
        data = value.to_bytes(length * 2, "big", signed=signed)
        return GrowattInverter.bytes_to_registers(data)

    @staticmethod
    def registers_to_char(registers: List[int], start: int = 0, length: int = 1) -> str:
        """Convert multiple registers to a string.

        Args:
            registers (List[int]): The registers to convert.
            start (int): The start index of the registers.
            length (int): The number of registers to convert."""
        data = GrowattInverter.registers_to_bytes(registers, start, length)
        return data.decode("utf-8")

    @staticmethod
    def generic_read_postprocess(
        registers: List[int], start: int, length: int, type_: RegType
    ):
        """Generic postprocess function for read registers.

        Args:
            registers (List[int]): The registers to read.
            start (int): The start index of the registers.
            length (int): The number of registers to read.
            type_ (RegType): The register type."""
        match type_:
            case RegType.UINT | RegType.INT:
                return GrowattInverter.combine_registers(
                    registers, start, length, signed=type_ == RegType.INT
                )
            case RegType.CHAR:
                return GrowattInverter.registers_to_char(registers, start, length)
            case _:
                raise ValueError("Invalid register type")

    def read_status(self):
        """Read the system status and other information from the inverter."""
        row = self.client.read_input_registers(0, 91)
        reg = row.registers
        info = {}
        for key, value in InputRegisters.items():
            start, length, type_, postprocess = value
            info[key] = self.generic_read_postprocess(reg, start, length, type_)
            info[key] = postprocess(info[key])

        return info

    def read_config(self):
        """Read the system configuration from the inverter."""
        row1 = self.client.read_holding_registers(0, 101)  # 0-100
        row2 = self.client.read_holding_registers(101, 62)  # 101-162
        reg = row1.registers + row2.registers
        info = {}
        for key, value in HoldingAndWriteRegisters.items():
            start, length, type_, readpostprocess, _ = value
            info[key] = self.generic_read_postprocess(reg, start, length, type_)
            info[key] = readpostprocess(info[key])

        return info

    def write_config(self, key: str, value: Union[str, int, float]):
        """Schedule a config write to the inverter to be processed by the write thread.

        Args:
            key (str): The configuration key to write.
            value (Union[str, int, float]): The value to write."""
        try:
            start, length, type_, _, writepreprocess = HoldingAndWriteRegisters[key]
        except KeyError as exc:
            raise KeyError("Invalid key") from exc
        if not writepreprocess:
            raise ValueError("Register is not writeable")

        try:
            value = writepreprocess(value)
        except (ValueError, KeyError) as exc:
            raise ValueError("Invalid value") from exc

        match type_:
            case RegType.UINT | RegType.INT:
                values = self.uncombine_registers(
                    value, length, signed=type_ == RegType.INT
                )
            case RegType.CHAR:
                values = list(value.encode("utf-8"))
                values = [
                    values[i] << 8 | values[i + 1] if i + 1 < len(values) else values[i]
                    for i in range(0, len(values), 2)
                ]
                values += [0] * (length - len(values))  # pad with zeros
                if len(values) != length:
                    raise ValueError("Invalid value length")
            case _:
                raise ValueError("Invalid register type")

        self.write_queue.put((start, values))


@dataclass
class GrowattHTTPAuth:
    """Growatt HTTP authentication dataclass."""

    username: str
    password_hash: str
    password_salt: str


class GrowattHTTPHandler(BaseHTTPRequestHandler):
    """
    HTTP request handler for the Growatt inverter.
    """

    server_version = ""
    sys_version = ""

    def __init__(
        self,
        inverter: GrowattInverter,
        timeout: int,
        x_forwarded_for: bool,
        auth: GrowattHTTPAuth,
        *args,
        **kwargs,
    ):
        """Initialize the handler.

        Args:
            inverter (GrowattInverter): The Growatt inverter.
            timeout (int): The timeout in seconds for the request.
            x_forwarded_for (bool): Whether to use the X-Forwarded-For header.
            auth (GrowattHTTPAuth): The authentication data.
            *args: Passed to the BaseHTTPRequestHandler constructor.
            **kwargs: Passed to the BaseHTTPRequestHandler constructor."""
        self.inverter = inverter
        self.timeout = timeout
        self.x_forwarded_for = x_forwarded_for
        self.auth = auth
        super().__init__(*args, **kwargs)

    @override
    def handle(self):
        """Handle multiple requests if necessary but catch ConnectionResetError."""
        try:
            super().handle()
        except ConnectionResetError:
            pass

    @override
    def address_string(self) -> str:
        """Return the client address with X-Forwarded-For support."""
        if (
            self.x_forwarded_for
            and hasattr(self, "headers")
            and "X-Forwarded-For" in self.headers
        ):
            return self.headers["X-Forwarded-For"]
        return super().address_string()

    @override
    def version_string(self):
        """Return the server software version string."""
        return "Growatt/1.0"

    @staticmethod
    def hash_password(password: str, salt: str) -> str:
        """Hashes the password using the given salt."""
        return hmac.new(
            bytes(salt, "utf-8"), bytes(password, "utf-8"), hashlib.sha1
        ).hexdigest()

    def send_final_response(
        self, code: int, headers: Dict[str, str], body: Optional[bytes]
    ):
        """Set the response code, headers, and body with automatic Content-Length.

        Args:
            code (int): The HTTP status code.
            headers (Dict[str, str]): The response headers.
            body (Optional[bytes]): The response body.
        """
        self.send_response(code)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        if "Connection" not in headers:
            self.send_header("Connection", "close")
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    @override
    def send_error(
        self, code: int, message: Optional[str] = None, explain: Optional[str] = None
    ) -> None:
        """Send and log an error reply.

        Args:
            code (int): The HTTP error code.
            message (str, optional): A simple one-line reason phrase. Defaults to None.
            explain (str, optional): A detailed message explaining the error. Defaults to None.

        This sends an error response (so it must be called before any
        output has been generated), logs the error, and finally sends
        a piece of JSON explaining the error to the user."""
        try:
            shortmsg, longmsg = self.responses[code]
        except KeyError:
            shortmsg, longmsg = "???", "???"
        if message is None:
            message = shortmsg
        if explain is None:
            explain = longmsg
        self.log_error("code %d, message %s", code, message)

        # Message body is omitted for cases described in:
        #  - RFC7230: 3.3. 1xx, 204(No Content), 304(Not Modified)
        #  - RFC7231: 6.3.6. 205(Reset Content)
        body = None
        if code >= 200 and code not in (
            HTTPStatus.NO_CONTENT,
            HTTPStatus.RESET_CONTENT,
            HTTPStatus.NOT_MODIFIED,
        ):
            content = {
                "code": code,
                "message": message,
                "explain": explain,
            }
            body = json_dumps(content, indent=4).encode("utf-8")

        self.send_final_response(
            code,
            {"Content-Type": CONTENT_TYPE_JSON},
            body,
        )

    def check_auth(self, username: str, password: str) -> bool:
        """Validates the password against the stored hash.

        Args:
            username (str): The username.
            password (str): The password."""
        return (
            username == self.auth.username
            and GrowattHTTPHandler.hash_password(password, self.auth.password_salt)
            == self.auth.password_hash
        )

    def validate_basic_auth_header(self, authorization_header: str) -> bool:
        """Validate the Authorization header.

        Args:
            authorization_header (str): The Authorization header."""
        try:
            auth_type, auth_string = authorization_header.split(" ", 1)
            if auth_type.lower() != "basic":
                return False
            auth_string = base64.b64decode(auth_string).decode("utf-8")
            if ":" not in auth_string:
                return False
            return self.check_auth(*auth_string.split(":", 1))
        except binascii.Error:
            return False

    @staticmethod
    def auth_required(func):
        """Decorator to require Basic authentication."""

        def wrapper(self: "GrowattHTTPHandler", *args, **kwargs):
            """Wrapper function."""
            if (
                hasattr(self, "headers")
                and "Authorization" in self.headers
                and self.validate_basic_auth_header(self.headers["Authorization"])
            ):
                return func(self, *args, **kwargs)

            self.send_final_response(
                HTTPStatus.UNAUTHORIZED,
                {
                    "Content-Type": CONTENT_TYPE_TEXT,
                    "WWW-Authenticate": 'Basic realm="Growatt"',
                },
                b"Unauthorized",
            )
            return None

        return wrapper

    def parse_path_qs(self):
        """Parse the path and query string.

        Returns:
            Tuple[str, Dict[str, List[str]]]: The path and query string."""
        path = self.path.split("?")
        qs = parse_qs(path[1]) if len(path) > 1 else {}
        path = path[0]  # ignore query string
        return path, qs

    @auth_required
    @override
    def do_HEAD(self):  # pylint: disable=invalid-name
        """Handle HEAD requests."""
        self.do_GET()

    @auth_required
    @override
    def do_GET(self):  # pylint: disable=invalid-name
        """Handle GET requests."""
        path, qs = self.parse_path_qs()

        if "_method" in qs:
            match method := qs["_method"][0].upper():
                case "PUT":
                    self.do_PUT()
                case "GET":
                    pass  # continue with GET
                case "HEAD":
                    self.send_error(
                        HTTPStatus.BAD_REQUEST,
                        "HEAD method not supported for _method",
                    )
                case _:
                    self.send_error(
                        HTTPStatus.NOT_IMPLEMENTED,
                        f"Unsupported method ({method!r})",
                    )
            return

        match path:
            case "/":
                self.send_final_response(
                    HTTPStatus.OK,
                    {"Content-Type": CONTENT_TYPE_HTML},
                    INDEX_HTML,
                )
            case "/status":
                try:
                    self.send_final_response(
                        HTTPStatus.OK,
                        {"Content-Type": CONTENT_TYPE_JSON},
                        json_dumps(self.inverter.read_status(), indent=4).encode(
                            "utf-8"
                        ),
                    )
                except ModbusException as exc:
                    self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            case "/config":
                try:
                    self.send_final_response(
                        HTTPStatus.OK,
                        {"Content-Type": CONTENT_TYPE_JSON},
                        json_dumps(self.inverter.read_config(), indent=4).encode(
                            "utf-8"
                        ),
                    )
                except ModbusException as exc:
                    self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            case _:
                self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    @auth_required
    @override
    def do_PUT(self):  # pylint: disable=invalid-name
        """Handle PUT requests."""
        path, qs = self.parse_path_qs()

        if path != "/config":
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
            return

        if "key" not in qs or "value" not in qs:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid query")
            return

        key = qs["key"][0]
        value = qs["value"][0]

        try:
            value = float(value)
        except ValueError:
            pass  # allow string value if conversion fails

        try:
            value_int = int(value)
            if abs(value_int - value) < 1e-3:
                value = value_int
        except ValueError:
            pass  # allow float value if conversion fails

        try:
            self.inverter.write_config(key, value)
            self.send_final_response(
                HTTPStatus.OK,
                {"Content-Type": CONTENT_TYPE_TEXT},
                b"OK",
            )
        except (KeyError, ValueError) as exc:
            self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except ModbusException as exc:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))


def growatt_http_handler_factory(
    *,
    inverter: GrowattInverter,
    timeout: int,
    x_forwarded_for: bool,
    auth: GrowattHTTPAuth,
):
    """Factory function to create a GrowattHTTPHandler instance.

    Args:
        inverter (GrowattInverter): The Growatt inverter.
        timeout (int): The timeout in seconds for the request.
        x_forwarded_for (bool): Whether to use the X-Forwarded-For header.
        auth (GrowattHTTPAuth): The authentication dataclass."""
    return lambda *args, **kwargs: GrowattHTTPHandler(
        inverter, timeout, x_forwarded_for, auth, *args, **kwargs
    )


def main():
    """Main function."""
    inverter: Optional[GrowattInverter] = None
    http_server: Optional[HTTPServer] = None
    try:
        cfg = configparser.ConfigParser()
        cfg.read("config.ini")
        modbus_port = cfg.get("MODBUS", "PORT")
        web_user = cfg.get("WEB", "USER")
        web_pass_salt = cfg.get("WEB", "PASS_SALT")
        web_pass_hash = cfg.get("WEB", "PASS_HASH")
        web_addr = cfg.get("WEB", "ADDR")
        web_port = cfg.getint("WEB", "PORT")
        web_timeout = cfg.getint("WEB", "TIMEOUT_SEC")
        web_x_forwarded_for = cfg.getboolean("WEB", "X_FORWARDED_FOR")

        sys.stderr.write(f"[INFO] Inverter port set to {modbus_port}\n")
        sys.stderr.write(f"[INFO] HTTP Server listening on {web_addr}:{web_port}\n")
        inverter = GrowattInverter(modbus_port)
        http_handler = growatt_http_handler_factory(
            inverter=inverter,
            timeout=web_timeout,
            x_forwarded_for=web_x_forwarded_for,
            auth=GrowattHTTPAuth(
                username=web_user,
                password_hash=web_pass_hash,
                password_salt=web_pass_salt,
            ),
        )
        http_server = HTTPServer((web_addr, web_port), http_handler)
        inverter.connect()
        http_server.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # pylint: disable=broad-except
        sys.stderr.write(f"[ERROR] {exc}\n")
    finally:
        try:
            if http_server:
                http_server.server_close()
        except Exception as exc:  # pylint: disable=broad-except
            sys.stderr.write(f"[ERROR] Failed to close server: {exc}\n")

        try:
            if inverter:
                inverter.close()
        except Exception as exc:  # pylint: disable=broad-except
            sys.stderr.write(f"[ERROR] Failed to close inverter: {exc}\n")


if __name__ == "__main__":
    main()
