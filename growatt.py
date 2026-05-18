#!/usr/bin/env python3

"""Allows to read and write data from Growatt inverters using Modbus RTU over RS485."""

# pylint: disable=too-many-lines

import configparser
import logging
import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from json import dumps as json_dumps
from json import loads as json_loads
from threading import RLock
from time import perf_counter, sleep
from typing import Any, Callable, Dict, List, Optional, Union

import paho.mqtt.client as mqtt
from pymodbus.client import ModbusSerialClient as ModbusClient
from pymodbus.exceptions import ModbusException


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


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


def optional_str(value: Optional[str]) -> Optional[str]:
    """Parse an optional string config value."""

    if value is None:
        return None
    value = value.strip()
    if value.lower() in ("", "none", "null", "false"):
        return None
    return value


def parse_config_value(value: str) -> Union[str, int, float]:
    """Parse a query value into a finite scalar for register preprocessing."""

    try:
        parsed_value: Union[str, int, float] = float(value)
    except ValueError:
        return value

    if not math.isfinite(parsed_value):
        raise ValueError("Invalid value")

    value_int = int(parsed_value)
    if abs(value_int - parsed_value) < 1e-3:
        return value_int
    return parsed_value


## Register Types ##
class RegType(Enum):
    """Enum for register types."""

    UINT = 0
    INT = 1
    CHAR = 2


## Input Registers ##
SYSTEM_STATUS_R = {
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
    13: "PV Discharging",
    14: "PV&Battery Discharging",
    15: "Gen Charging",
    16: "Gen Charging+Gen Bypass",
    17: "PV&Gen Charging",
    18: "PV&Gen Charging+Gen Bypass",
    19: "PV Charging+Gen Bypass",
    20: "Gen Bypass",
    21: "PV Export to Grid",
    22: "PV Export to Grid+Loads Supporting",
    23: "PV Charging+Export to Grid",
    24: "PV Charging+Export to Grid+Loads Supporting",
    25: "Battery Export to Grid",
    26: "Battery Export to Grid+Loads Supporting",
    27: "Battery&PV Export to Grid",
    28: "Battery&PV Export to Grid+Loads Supporting",
}
INPUT_REGISTERS = {
    # Register: (Start, Length, Type, PostProcess)
    "SystemStatus": (0, 1, RegType.UINT, lambda x: SYSTEM_STATUS_R[x]),
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
    "ACInputWatt": (
        36,
        2,
        RegType.INT,
        lambda x: x / 10,
    ),  # > 0: From Grid, < 0: To Grid
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
    "BatteryWatt": (
        77,
        2,
        RegType.INT,
        lambda x: x / 10,
    ),  # > 0: Discharge, < 0: Charge
    "SlaveExistCount": (79, 1, RegType.UINT, int),
    "MpptFanSpeedPercent": (81, 1, RegType.UINT, int),
    "InvFanSpeedPercent": (82, 1, RegType.UINT, int),
    "TotalChargeAmps": (83, 1, RegType.UINT, lambda x: x / 10),
    "TotalDischargeAmps": (84, 1, RegType.UINT, lambda x: x / 10),
    "OPDischargeEnergyTodaykWh": (85, 2, RegType.UINT, lambda x: x / 10),
    "OPDischargeEnergyTotalkWh": (87, 2, RegType.UINT, lambda x: x / 10),
    "ParaChargeAmps": (90, 1, RegType.UINT, lambda x: x / 10),
    "ParallelStatus": (91, 1, RegType.UINT, int),
    "GeneratorEnergyTodaykWh": (92, 2, RegType.UINT, lambda x: x / 10),
    "GeneratorEnergyTotalkWh": (94, 2, RegType.UINT, lambda x: x / 10),
    "GeneratorWatt": (96, 1, RegType.UINT, int),
    "GeneratorVolt": (97, 1, RegType.UINT, lambda x: x / 10),
    "BatteryChargeEnergyTodaykWh": (98, 2, RegType.UINT, lambda x: x / 10),
    "BatteryChargeEnergyTotalkWh": (100, 2, RegType.UINT, lambda x: x / 10),
    "CTInputWatt": (102, 2, RegType.UINT, lambda x: x / 10),
    "CTLoadWatt": (104, 2, RegType.UINT, lambda x: x / 10),
    "CTLoadPercent": (106, 1, RegType.UINT, lambda x: x / 10),
    "TransformerTempC": (107, 1, RegType.INT, lambda x: x / 10),
    "LLCTempC": (108, 1, RegType.INT, lambda x: x / 10),
    "LLCBusVolt": (109, 1, RegType.UINT, lambda x: x / 10),
    "LLCBatteryVolt": (110, 1, RegType.UINT, lambda x: x / 100),
    "EnvTempC": (111, 1, RegType.INT, lambda x: x / 10),
    "BMSStatus": (200, 1, RegType.UINT, int),
    "BMSErrorOld": (201, 1, RegType.UINT, int),
    "BMSWarnInfoOld": (202, 1, RegType.UINT, int),
    "BMSSOC": (203, 1, RegType.UINT, int),
    "BMSBatteryVolt": (204, 1, RegType.UINT, lambda x: x / 100),
    "BMSBatteryAmps": (205, 1, RegType.INT, lambda x: x / 10),
    "BMSBatteryTempC": (206, 1, RegType.INT, lambda x: x / 10),
    "BMSMaxChargeAmps": (207, 1, RegType.UINT, lambda x: x / 10),
    "BMSCVVolt": (208, 1, RegType.UINT, lambda x: x / 100),
    "BMSInfo": (209, 1, RegType.UINT, int),
    "BMSPackInfo": (210, 1, RegType.UINT, int),
    "BMSUsingCapacity": (211, 1, RegType.UINT, int),
    "BMSCell1Volt": (212, 1, RegType.UINT, lambda x: x / 1000),
    "BMSCell2Volt": (213, 1, RegType.UINT, lambda x: x / 1000),
    "BMSCell3Volt": (214, 1, RegType.UINT, lambda x: x / 1000),
    "BMSCell4Volt": (215, 1, RegType.UINT, lambda x: x / 1000),
    "BMSCell5Volt": (216, 1, RegType.UINT, lambda x: x / 1000),
    "BMSCell6Volt": (217, 1, RegType.UINT, lambda x: x / 1000),
    "BMSCell7Volt": (218, 1, RegType.UINT, lambda x: x / 1000),
    "BMSCell8Volt": (219, 1, RegType.UINT, lambda x: x / 1000),
    "BMSCell9Volt": (220, 1, RegType.UINT, lambda x: x / 1000),
    "BMSCell10Volt": (221, 1, RegType.UINT, lambda x: x / 1000),
    "BMSCell11Volt": (222, 1, RegType.UINT, lambda x: x / 1000),
    "BMSCell12Volt": (223, 1, RegType.UINT, lambda x: x / 1000),
    "BMSCell13Volt": (224, 1, RegType.UINT, lambda x: x / 1000),
    "BMSCell14Volt": (225, 1, RegType.UINT, lambda x: x / 1000),
    "BMSCell15Volt": (226, 1, RegType.UINT, lambda x: x / 1000),
    "BMSCell16Volt": (227, 1, RegType.UINT, lambda x: x / 1000),
    "BMSModuleID": (228, 1, RegType.UINT, int),
    "BMSModuleTotalVolt": (229, 1, RegType.INT, lambda x: x / 100),
    "BMSModuleTotalAmps": (230, 1, RegType.INT, lambda x: x / 10),
    "BMSModuleSOC": (231, 1, RegType.UINT, int),
    "BMSModuleStatus": (232, 1, RegType.UINT, int),
    "BMSBatProtect1_2": (233, 1, RegType.UINT, int),
    "BMSBatWarnInfo1_2": (234, 1, RegType.UINT, int),
    "BMSPackNumber": (235, 1, RegType.UINT, int),
    "BMSBatDePowerReason": (236, 1, RegType.UINT, int),
    "BMSSOH": (237, 1, RegType.UINT, int),
    "BMSGaugeRM10mAh": (238, 1, RegType.UINT, int),
    "BMSGaugeFCC10mAh": (239, 1, RegType.UINT, int),
    "BMSDeltaVolt": (240, 1, RegType.UINT, lambda x: x / 1000),
    "BMSCycleCount": (241, 1, RegType.UINT, int),
    "BMSRequestOrBatteryType": (242, 1, RegType.UINT, int),
    "BMSMaximumCellVolt": (243, 1, RegType.UINT, lambda x: x / 1000),
    "BMSMinimumCellVolt": (244, 1, RegType.UINT, lambda x: x / 1000),
    "BMSMaxMinCellVoltageNumber": (245, 1, RegType.UINT, int),
    "BMSProtectPackID": (246, 1, RegType.UINT, int),
    "BMSManufacturerName": (247, 1, RegType.CHAR, str),
    "BMSHardwareVersion": (248, 1, RegType.UINT, int),
    "BMSSoftwareVersion01": (249, 1, RegType.UINT, int),
    "BMSParallelHighSoftwareVersion": (250, 1, RegType.UINT, int),
    "BMSMaxCellTempC": (251, 1, RegType.INT, lambda x: x / 10),
    "BMSMinCellTempC": (252, 1, RegType.INT, lambda x: x / 10),
    "BMSMaxMinCellTempSerialNum": (253, 1, RegType.UINT, int),
    "BMSMaxMinSOC": (254, 1, RegType.UINT, int),
    "BMSTotalCellNumber": (255, 1, RegType.UINT, int),
    "BMSBatProtect3_4": (256, 1, RegType.UINT, int),
    "BMSBatProtect5": (257, 1, RegType.UINT, int),
    "BMSBatWarnInfo3": (258, 1, RegType.UINT, int),
    "BMSUpdateStatus": (259, 1, RegType.UINT, int),
    "BMSSoftwareVersion23": (260, 1, RegType.CHAR, str),
    "BMSSoftwareVersion45": (261, 1, RegType.CHAR, str),
    "BMSBatterySerialNumberID": (262, 1, RegType.UINT, int),
    "BMSBatterySerialNumber": (263, 10, RegType.CHAR, str),
    "BMSModuleID2": (273, 1, RegType.UINT, int),
    "BMSModule2MaxVolt": (274, 1, RegType.UINT, lambda x: x / 100),
    "BMSModule2MinVolt": (275, 1, RegType.UINT, lambda x: x / 100),
    "BMSModule2MaxTempC": (276, 1, RegType.INT, lambda x: x - 40),
    "BMSModule2MinTempC": (277, 1, RegType.INT, lambda x: x - 40),
    "BMSDoStatus": (278, 1, RegType.UINT, int),
    "BMSDischargeBatteryNumber": (279, 1, RegType.UINT, int),
    "BMSDischargeEnergykWh": (280, 2, RegType.UINT, int),
    "BMSChargeBatteryNumber": (282, 1, RegType.UINT, int),
    "BMSChargeEnergykWh": (283, 2, RegType.UINT, int),
}

## Holding Registers ##
OUTPUT_CONFIG_R = {0: "SBU", 1: "SOL", 2: "UTI", 3: "SUB"}
OUTPUT_CONFIG_W = {v: k for k, v in OUTPUT_CONFIG_R.items()}
CHARGE_CONFIG_R = {0: "PV First", 1: "PV&UTI", 2: "PV Only"}
CHARGE_CONFIG_W = {v: k for k, v in CHARGE_CONFIG_R.items()}
PV_MODEL_R = {0: "Independent", 1: "Parallel"}
PV_MODEL_W = {v: k for k, v in PV_MODEL_R.items()}
AC_IN_MODEL_R = {0: "APL", 1: "UPS", 2: "GEN"}
AC_IN_MODEL_W = {v: k for k, v in AC_IN_MODEL_R.items()}
OUTPUT_VOLT_TYPE_R = {
    0: "208VAC",
    1: "230VAC",
    2: "240VAC",
    3: "220VAC",
    4: "100VAC",
    5: "110VAC",
    6: "120VAC",
}
OUTPUT_VOLT_TYPE_W = {v: k for k, v in OUTPUT_VOLT_TYPE_R.items()}
OUTPUT_FREQ_TYPE_R = {0: "50Hz", 1: "60Hz"}
OUTPUT_FREQ_TYPE_W = {v: k for k, v in OUTPUT_FREQ_TYPE_R.items()}
OVER_LOAD_RESTART_R = {0: "Yes", 1: "No", 2: "Switch to UTI"}
OVER_LOAD_RESTART_W = {v: k for k, v in OVER_LOAD_RESTART_R.items()}
OVER_TEMP_RESTART_R = {0: True, 1: False}
OVER_TEMP_RESTART_W = {v: k for k, v in OVER_TEMP_RESTART_R.items()}
BATTERY_TYPE_R = {0: "AGM", 1: "FLD", 2: "USE", 3: "Lithium", 4: "USE2"}
BATTERY_TYPE_W = {v: k for k, v in BATTERY_TYPE_R.items()}
AGING_MODE_R = {0: "Normal", 1: "Aging"}
AGING_MODE_W = {v: k for k, v in AGING_MODE_R.items()}
SAFETY_TYPE_R = {1: "Standard", 2: "ETL", 3: "AS4777", 4: "CQC", 5: "VDE4105"}
SAFETY_TYPE_W = {v: k for k, v in SAFETY_TYPE_R.items()}
ON_OFF_R = {0x0000: "Output enable", 0x0100: "Output disable"}
HOLDING_AND_WRITE_REGISTERS = {
    # Register: (Start, Length, Type, ReadPostProcess, WritePreProcess (None if not writeable))
    "OnOff": (0, 1, RegType.UINT, lambda x: ON_OFF_R[x], None),
    "OutputConfig": (
        1,
        1,
        RegType.UINT,
        lambda x: OUTPUT_CONFIG_R[x],
        lambda x: OUTPUT_CONFIG_W[x],
    ),
    "ChargeConfig": (
        2,
        1,
        RegType.UINT,
        lambda x: CHARGE_CONFIG_R[x],
        lambda x: CHARGE_CONFIG_W[x],
    ),
    "UtiOutStart": (3, 1, RegType.UINT, int, int),  # 0-23
    "UtiOutEnd": (4, 1, RegType.UINT, int, int),  # 0-23
    "UtiChargeStart": (5, 1, RegType.UINT, int, int),  # 0-23
    "UtiChargeEnd": (6, 1, RegType.UINT, int, int),  # 0-23
    "PVModel": (7, 1, RegType.UINT, lambda x: PV_MODEL_R[x], lambda x: PV_MODEL_W[x]),
    "ACInModel": (
        8,
        1,
        RegType.UINT,
        lambda x: AC_IN_MODEL_R[x],
        lambda x: AC_IN_MODEL_W[x],
    ),
    "FWVersion": (9, 3, RegType.CHAR, str, None),
    "FWVersion2": (12, 3, RegType.CHAR, str, None),
    "LCDLanguage": (15, 1, RegType.UINT, int, int),
    "GridV_Adj": (16, 1, RegType.UINT, int, None),
    "InvV_Adj": (17, 1, RegType.UINT, int, None),
    "OutputVoltType": (
        18,
        1,
        RegType.UINT,
        lambda x: OUTPUT_VOLT_TYPE_R[x],
        lambda x: OUTPUT_VOLT_TYPE_W[x],
    ),
    "OutputFreqType": (
        19,
        1,
        RegType.UINT,
        lambda x: OUTPUT_FREQ_TYPE_R[x],
        lambda x: OUTPUT_FREQ_TYPE_W[x],
    ),
    "OverLoadRestart": (
        20,
        1,
        RegType.UINT,
        lambda x: OVER_LOAD_RESTART_R[x],
        lambda x: OVER_LOAD_RESTART_W[x],
    ),
    "OverTempRestart": (
        21,
        1,
        RegType.UINT,
        lambda x: OVER_TEMP_RESTART_R[x],
        lambda x: OVER_TEMP_RESTART_W[str2bool(x)],
    ),
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
    "BatteryType": (
        39,
        1,
        RegType.UINT,
        lambda x: BATTERY_TYPE_R[x],
        lambda x: BATTERY_TYPE_W[x],
    ),
    "AgingMode": (
        40,
        1,
        RegType.UINT,
        lambda x: AGING_MODE_R[x],
        lambda x: AGING_MODE_W[x],
    ),
    "FunctionMask": (41, 1, RegType.UINT, int, int),
    "SafetyType": (
        42,
        1,
        RegType.UINT,
        lambda x: SAFETY_TYPE_R[x],
        lambda x: SAFETY_TYPE_W[x],
    ),
    "DTC": (43, 1, RegType.UINT, int, None),
    "SysYear": (45, 1, RegType.UINT, int, int),
    "SysMonth": (46, 1, RegType.UINT, int, int),
    "SysDay": (47, 1, RegType.UINT, int, int),
    "SysHour": (48, 1, RegType.UINT, int, int),
    "SysMin": (49, 1, RegType.UINT, int, int),
    "SysSec": (50, 1, RegType.UINT, int, int),
    "HoldingChipSelect": (51, 1, RegType.UINT, int, None),
    "uwAcVHighL": (52, 1, RegType.UINT, int, None),
    "uwAcVLowL": (53, 1, RegType.UINT, int, None),
    "uwAcFreqHighL": (54, 1, RegType.UINT, int, None),
    "uwAcFreqLowL": (55, 1, RegType.UINT, int, None),
    "HoldingVar1Setting": (56, 1, RegType.UINT, int, None),
    "DebugModeEnable": (57, 1, RegType.UINT, bool, None),
    "ManufacturerInfo": (59, 8, RegType.CHAR, str, None),
    "ControlFWBuildNo2": (67, 1, RegType.UINT, int, None),
    "ControlFWBuildNo1": (68, 1, RegType.UINT, int, None),
    "ComFWBuildNo2": (69, 1, RegType.UINT, int, None),
    "ComFWBuildNo1": (70, 1, RegType.UINT, int, None),
    "SysWeekly": (72, 1, RegType.UINT, int, int),
    "ModbusVersion": (73, 1, RegType.UINT, int, None),
    "SCCComMode": (75, 1, RegType.UINT, int, None),
    "RateWatt": (76, 2, RegType.UINT, lambda x: x / 10, None),
    "RateVA": (78, 2, RegType.UINT, lambda x: x / 10, None),
    "ComboardVer": (80, 1, RegType.UINT, int, None),
    "uwBatPieceNum": (81, 1, RegType.UINT, int, int),
    "wBatLowCutOff": (82, 1, RegType.UINT, lambda x: x / 10, None),
    "MaxGeneratorChargeAmps": (83, 1, RegType.UINT, int, None),
    "NomGridVRaw": (84, 1, RegType.UINT, int, None),
    "NomGridFreqRaw": (85, 1, RegType.UINT, int, None),
    "NomBatVRaw": (86, 1, RegType.UINT, int, None),
    "NomPVCurrRaw": (87, 1, RegType.UINT, int, None),
    "NomAcChgCurrRaw": (88, 1, RegType.UINT, int, None),
    "NomOpVRaw": (89, 1, RegType.UINT, int, None),
    "NomOpFreqRaw": (90, 1, RegType.UINT, int, None),
    "NomOpPowRaw": (91, 1, RegType.UINT, int, None),
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
    "uwEqEnable": (108, 1, RegType.UINT, int, None),
    "uwEqChgVRaw": (109, 1, RegType.UINT, int, None),
    "uwEqTime": (110, 1, RegType.UINT, int, None),
    "uwEqTimeOut": (111, 1, RegType.UINT, int, None),
    "uwEqInterval": (112, 1, RegType.UINT, int, None),
    "uwMaxDisChgCurrRaw": (113, 1, RegType.UINT, int, None),
    "FaultRestartEnable": (114, 1, RegType.UINT, bool, None),
    "FeedEnable": (115, 1, RegType.UINT, bool, None),
    "LoadFirst": (116, 1, RegType.UINT, int, None),
    "FeedRange": (117, 1, RegType.UINT, int, None),
    "BatteryFeedEnable": (118, 1, RegType.UINT, bool, None),
    "FeedPowerLimitkW": (119, 1, RegType.UINT, lambda x: x / 10, None),
    "BatteryFeedAmps": (120, 1, RegType.UINT, int, None),
    "BatteryFeedVoltLoss": (121, 1, RegType.UINT, lambda x: x / 10, None),
    "BatteryFeedVoltBack": (122, 1, RegType.UINT, lambda x: x / 10, None),
    "BatteryFeedSOCLoss": (123, 1, RegType.UINT, int, None),
    "BatteryFeedSOCBack": (124, 1, RegType.UINT, int, None),
    "BatteryFeedTimeStart1": (125, 1, RegType.UINT, int, None),
    "BatteryFeedTimeEnd1": (126, 1, RegType.UINT, int, None),
    "BatteryFeedTimeStart2": (127, 1, RegType.UINT, int, None),
    "BatteryFeedTimeEnd2": (128, 1, RegType.UINT, int, None),
    "BatteryFeedTimeStart3": (129, 1, RegType.UINT, int, None),
    "BatteryFeedTimeEnd3": (130, 1, RegType.UINT, int, None),
    "GridChargeTimeStart1": (131, 1, RegType.UINT, int, None),
    "GridChargeTimeEnd1": (132, 1, RegType.UINT, int, None),
    "GridChargeTimeStart2": (133, 1, RegType.UINT, int, None),
    "GridChargeTimeEnd2": (134, 1, RegType.UINT, int, None),
    "GridChargeTimeStart3": (135, 1, RegType.UINT, int, None),
    "GridChargeTimeEnd3": (136, 1, RegType.UINT, int, None),
    "MaxGeneratorRunHours": (137, 1, RegType.UINT, int, None),
    "LiBatteryChargeIntervalEnable": (138, 1, RegType.UINT, bool, None),
    "LiBatteryChargeInterval": (139, 1, RegType.UINT, int, None),
    "NgRelayEnable": (140, 1, RegType.UINT, bool, None),
    "GridAlwaysOnEnable": (141, 1, RegType.UINT, bool, None),
    "Output2TimeStart1": (142, 1, RegType.UINT, int, None),
    "Output2TimeEnd1": (143, 1, RegType.UINT, int, None),
    "Output2TimeStart2": (144, 1, RegType.UINT, int, None),
    "Output2TimeEnd2": (145, 1, RegType.UINT, int, None),
    "Output2TimeStart3": (146, 1, RegType.UINT, int, None),
    "Output2TimeEnd3": (147, 1, RegType.UINT, int, None),
    "Output2VoltLoss": (148, 1, RegType.UINT, lambda x: x / 10, None),
    "Output2SOCLoss": (149, 1, RegType.UINT, int, None),
    "Output2VoltBack": (150, 1, RegType.UINT, lambda x: x / 10, None),
    "Output2SOCBack": (151, 1, RegType.UINT, int, None),
    "PVLowLimitWattkW": (152, 1, RegType.UINT, lambda x: x / 10, None),
    "MenuBackEnable": (153, 1, RegType.UINT, bool, None),
    "BMSErrorWorkEnable": (154, 1, RegType.UINT, bool, None),
    "ExternalCTEnable": (155, 1, RegType.UINT, bool, None),
    "ExternalCTSampleRate": (156, 1, RegType.UINT, int, None),
    "ShavingPowerkW": (157, 1, RegType.UINT, lambda x: x / 10, None),
    "ExportLimitPowerkW": (158, 1, RegType.UINT, lambda x: x / 10, None),
    "TypicalSetup": (159, 1, RegType.UINT, int, None),
    "ETLEnable": (160, 1, RegType.UINT, bool, None),
    "PVIsoEnable": (161, 1, RegType.UINT, bool, None),
    "GFCIFastProtectEnable": (162, 1, RegType.UINT, bool, None),
    "FeedVoltHighLoss": (163, 1, RegType.UINT, int, None),
    "FeedVoltLowLoss": (164, 1, RegType.UINT, int, None),
    "FeedFreqHighLoss": (165, 1, RegType.UINT, lambda x: x / 10, None),
    "FeedFreqLowLoss": (166, 1, RegType.UINT, lambda x: x / 10, None),
    "PVDCSourceEnable": (167, 1, RegType.UINT, bool, None),
    "ShavingEnable": (168, 1, RegType.UINT, bool, None),
    "DryContactEnable": (169, 1, RegType.UINT, int, None),
    "NewSerialNumber": (209, 15, RegType.CHAR, str, None),
    "GridHighVoltLoadReductionStart": (300, 1, RegType.UINT, lambda x: x / 10, None),
    "GridHighVoltLoadReductionEnd": (301, 1, RegType.UINT, lambda x: x / 10, None),
    "GridHighFreqLoadReductionStart": (302, 1, RegType.UINT, lambda x: x / 100, None),
    "GridHighFreqLoadReductionEnd": (303, 1, RegType.UINT, lambda x: x / 100, None),
    "GridLowFreqLoadReductionStart": (304, 1, RegType.UINT, lambda x: x / 1000, None),
    "GridLowFreqLoadReductionEnd": (305, 1, RegType.UINT, lambda x: x / 1000, None),
    "UnderfrequencyLoadingSlope": (306, 1, RegType.UINT, lambda x: x / 1000, None),
    "OverfrequencyLoadingSlope": (307, 1, RegType.UINT, lambda x: x / 1000, None),
    "GridHighVoltLoadReductionWatt1": (308, 1, RegType.UINT, int, None),
    "GridHighVoltLoadReductionWatt2": (309, 1, RegType.INT, int, None),
    "PFModelSet": (310, 1, RegType.UINT, int, None),
    "PowerFactorSet": (311, 1, RegType.INT, lambda x: x / 1000, None),
    "GridVoltLowStartup": (312, 1, RegType.UINT, lambda x: x / 10, None),
    "GridVoltHighStartup": (313, 1, RegType.UINT, lambda x: x / 10, None),
    "GridFreqLowStartup": (314, 1, RegType.UINT, lambda x: x / 100, None),
    "GridFreqHighStartup": (315, 1, RegType.UINT, lambda x: x / 100, None),
    "VoltLowLossPercent1": (316, 1, RegType.UINT, int, None),
    "VoltLowLossPercent2": (317, 1, RegType.UINT, int, None),
    "VoltLowLossPercent3": (318, 1, RegType.UINT, int, None),
    "VoltHighLossPercent1": (320, 1, RegType.UINT, int, None),
    "VoltHighLossPercent2": (321, 1, RegType.UINT, int, None),
    "VoltHighLossPercent3": (322, 1, RegType.UINT, int, None),
    "FreqLowLoss1": (324, 1, RegType.UINT, lambda x: x / 100, None),
    "FreqLowLoss2": (325, 1, RegType.UINT, lambda x: x / 100, None),
    "FreqLowLoss3": (326, 1, RegType.UINT, lambda x: x / 100, None),
    "FreqLowLoss4": (327, 1, RegType.UINT, lambda x: x / 100, None),
    "FreqHighLoss1": (328, 1, RegType.UINT, lambda x: x / 100, None),
    "FreqHighLoss2": (329, 1, RegType.UINT, lambda x: x / 100, None),
    "FreqHighLoss3": (330, 1, RegType.UINT, lambda x: x / 100, None),
    "VoltLowLossTime1": (332, 1, RegType.UINT, lambda x: x / 10, None),
    "VoltLowLossTime2": (333, 1, RegType.UINT, lambda x: x / 10, None),
    "VoltLowLossTime3": (334, 1, RegType.UINT, lambda x: x / 10, None),
    "VoltHighLossTime1": (336, 1, RegType.UINT, lambda x: x / 10, None),
    "VoltHighLossTime2": (337, 1, RegType.UINT, lambda x: x / 10, None),
    "VoltHighLossTime3": (338, 1, RegType.UINT, lambda x: x / 10, None),
    "VoltReconnectTime": (339, 1, RegType.UINT, lambda x: x / 10, None),
    "FreqLowLossTime1": (340, 1, RegType.UINT, lambda x: x / 10, None),
    "FreqLowLossTime2": (341, 1, RegType.UINT, lambda x: x / 10, None),
    "FreqLowLossTime3": (342, 1, RegType.UINT, lambda x: x / 10, None),
    "FreqLowLossTime4": (343, 1, RegType.UINT, lambda x: x / 10, None),
    "FreqHighLossTime1": (344, 1, RegType.UINT, lambda x: x / 10, None),
    "FreqHighLossTime2": (345, 1, RegType.UINT, lambda x: x / 10, None),
    "FreqHighLossTime3": (346, 1, RegType.UINT, lambda x: x / 10, None),
    "FreqReconnectTime": (347, 1, RegType.UINT, lambda x: x / 10, None),
    "LVRT1Volt": (348, 1, RegType.UINT, lambda x: x / 10, None),
    "LVRT2Volt": (349, 1, RegType.UINT, lambda x: x / 10, None),
    "LVRT3Volt": (350, 1, RegType.UINT, lambda x: x / 10, None),
    "HVRT1Volt": (352, 1, RegType.UINT, lambda x: x / 10, None),
    "HVRT2Volt": (353, 1, RegType.UINT, lambda x: x / 10, None),
    "HVRT3Volt": (354, 1, RegType.UINT, lambda x: x / 10, None),
    "LVRTTime1": (356, 1, RegType.UINT, lambda x: x / 100, None),
    "LVRTTime2": (357, 1, RegType.UINT, lambda x: x / 100, None),
    "LVRTTime3": (358, 1, RegType.UINT, lambda x: x / 100, None),
    "HLVRTReconnectTime": (359, 1, RegType.UINT, lambda x: x / 10, None),
    "HVRTTime1": (360, 1, RegType.UINT, lambda x: x / 100, None),
    "HVRTTime2": (361, 1, RegType.UINT, lambda x: x / 100, None),
    "HVRTTime3": (362, 1, RegType.UINT, lambda x: x / 100, None),
    "LFRT1": (364, 1, RegType.UINT, lambda x: x / 100, None),
    "LFRT2": (365, 1, RegType.UINT, lambda x: x / 100, None),
    "LFRT3": (366, 1, RegType.UINT, lambda x: x / 100, None),
    "HFRT1": (368, 1, RegType.UINT, lambda x: x / 100, None),
    "HFRT2": (369, 1, RegType.UINT, lambda x: x / 100, None),
    "HFRT3": (370, 1, RegType.UINT, lambda x: x / 100, None),
    "LFRTTime1": (372, 1, RegType.UINT, lambda x: x / 100, None),
    "LFRTTime2": (373, 1, RegType.UINT, lambda x: x / 100, None),
    "LFRTTime3": (374, 1, RegType.UINT, lambda x: x / 100, None),
    "HFRTTime1": (376, 1, RegType.UINT, lambda x: x / 100, None),
    "HFRTTime2": (377, 1, RegType.UINT, lambda x: x / 100, None),
    "HFRTTime3": (378, 1, RegType.UINT, lambda x: x / 100, None),
    "LoadPOut1": (380, 1, RegType.UINT, int, None),
    "LoadPOut2": (381, 1, RegType.UINT, int, None),
    "LoadPOut3": (382, 1, RegType.UINT, int, None),
    "LoadQOut1": (384, 1, RegType.INT, int, None),
    "LoadQOut2": (385, 1, RegType.INT, int, None),
    "LoadQOut3": (386, 1, RegType.INT, int, None),
    "LoadPAbsorp1": (388, 1, RegType.UINT, int, None),
    "LoadPAbsorp2": (389, 1, RegType.UINT, int, None),
    "LoadPAbsorp3": (390, 1, RegType.UINT, int, None),
    "LoadQAbsorp1": (392, 1, RegType.INT, int, None),
    "LoadQAbsorp2": (393, 1, RegType.INT, int, None),
    "LoadQAbsorp3": (394, 1, RegType.INT, int, None),
    "ReactV1": (396, 1, RegType.UINT, lambda x: x / 10, None),
    "ReactV2": (397, 1, RegType.UINT, lambda x: x / 10, None),
    "ReactV3": (398, 1, RegType.UINT, lambda x: x / 10, None),
    "ReactV4": (399, 1, RegType.UINT, lambda x: x / 10, None),
    "ReactQ1Percent": (400, 1, RegType.INT, int, None),
    "ReactQ2Percent": (401, 1, RegType.INT, int, None),
    "ReactQ3Percent": (402, 1, RegType.INT, int, None),
    "ReactQ4Percent": (403, 1, RegType.INT, int, None),
    "PowerSlopeTime": (404, 1, RegType.UINT, int, None),
    "VoltVarOpenLoopResponseTime": (405, 1, RegType.UINT, lambda x: x / 10, None),
    "VrefModelFilterTime": (406, 1, RegType.UINT, lambda x: x / 10, None),
    "VoltWattOpenLoopResponseTime": (407, 1, RegType.UINT, lambda x: x / 10, None),
    "FreqDroopOpenLoopResponseTime": (408, 1, RegType.UINT, lambda x: x / 10, None),
    "StartDelayTime": (409, 1, RegType.UINT, int, None),
    "ReconnectTime": (410, 1, RegType.UINT, int, None),
    "DCIDetectPercent": (411, 1, RegType.UINT, lambda x: x / 100, None),
    "IslandProtectTime": (412, 1, RegType.UINT, lambda x: x / 10, None),
    "HLVRTEnable": (415, 1, RegType.UINT, bool, None),
    "HighVoltLoadReductionEnable": (416, 1, RegType.UINT, bool, None),
    "FreqLoadReductionEnable": (417, 1, RegType.UINT, bool, None),
    "AntiIslandEnable": (418, 1, RegType.UINT, bool, None),
    "AutoVRefEnable": (420, 1, RegType.UINT, bool, None),
    "MeterOrCTSwitch": (421, 1, RegType.UINT, int, None),
    "DCIAdjustmentEnable": (422, 1, RegType.UINT, bool, None),
    "IslandPWMEnable": (423, 1, RegType.UINT, bool, None),
    "SpecTypeValueEnable": (424, 1, RegType.UINT, bool, None),
    "VrefModelEnable": (425, 1, RegType.UINT, bool, None),
    "RoCoFEnable": (426, 1, RegType.UINT, bool, None),
}

CONFIG_SELECT_OPTIONS = {
    "OutputConfig": list(OUTPUT_CONFIG_W),
    "ChargeConfig": list(CHARGE_CONFIG_W),
    "PVModel": list(PV_MODEL_W),
    "ACInModel": list(AC_IN_MODEL_W),
    "OutputVoltType": list(OUTPUT_VOLT_TYPE_W),
    "OutputFreqType": list(OUTPUT_FREQ_TYPE_W),
    "OverLoadRestart": list(OVER_LOAD_RESTART_W),
    "BatteryType": list(BATTERY_TYPE_W),
    "AgingMode": list(AGING_MODE_W),
    "SafetyType": list(SAFETY_TYPE_W),
}

CONFIG_BOOLEAN_KEYS = {
    "OverTempRestart",
    "BuzzerEnable",
    "BypEnable",
    "PowSavingEnable",
    "SpowBalEnable",
    "ClrEnergyToday",
    "ClrEnergyAll",
    "BurnInTestEnable",
    "ManualStartEnable",
    "SciLossChkEnable",
    "BlightEnable",
    "AudioAlarmEnable",
}

MQTT_ENTITY_METADATA = {
    "SystemStatus": {"icon": "mdi:solar-power"},
    "FaultBit": {"icon": "mdi:alert-circle-outline"},
    "WarningBit": {"icon": "mdi:alert-outline"},
    "WarningBitHigh": {"icon": "mdi:alert-outline"},
    "WarningValue": {"icon": "mdi:alert-outline"},
    "DeviceTypeCode": {"icon": "mdi:identifier"},
    "WorkTimeTotalSeconds": {"icon": "mdi:timer-outline"},
    "OutputConfig": {"icon": "mdi:transmission-tower-export"},
    "ChargeConfig": {"icon": "mdi:battery-charging"},
    "UtiOutStart": {"icon": "mdi:clock-start", "unit_of_measurement": "h"},
    "UtiOutEnd": {"icon": "mdi:clock-end", "unit_of_measurement": "h"},
    "UtiChargeStart": {"icon": "mdi:battery-clock", "unit_of_measurement": "h"},
    "UtiChargeEnd": {"icon": "mdi:battery-clock", "unit_of_measurement": "h"},
    "PVModel": {"icon": "mdi:solar-panel"},
    "ACInModel": {"icon": "mdi:transmission-tower-import"},
    "FWVersion": {"icon": "mdi:chip"},
    "FWVersion2": {"icon": "mdi:chip"},
    "LCDLanguage": {"icon": "mdi:translate"},
    "SerialNumber": {"icon": "mdi:barcode"},
    "MoudleH": {"icon": "mdi:chip"},
    "MoudleL": {"icon": "mdi:chip"},
    "ComAddress": {"icon": "mdi:serial-port"},
    "FlashStart": {"icon": "mdi:flash"},
    "ResetUserInfo": {"icon": "mdi:account-sync-outline"},
    "ResetToFactory": {"icon": "mdi:factory"},
    "BatteryType": {"icon": "mdi:car-battery"},
    "AgingMode": {"icon": "mdi:timer-sand"},
    "FunctionMask": {"icon": "mdi:bitwise"},
    "SafetyType": {"icon": "mdi:shield-check-outline"},
    "DTC": {"icon": "mdi:alert-decagram-outline"},
    "SysYear": {"icon": "mdi:calendar"},
    "SysMonth": {"icon": "mdi:calendar-month"},
    "SysDay": {"icon": "mdi:calendar-today"},
    "SysHour": {"icon": "mdi:clock-outline", "unit_of_measurement": "h"},
    "SysMin": {"icon": "mdi:clock-outline", "unit_of_measurement": "min"},
    "SysSec": {"icon": "mdi:clock-outline", "unit_of_measurement": "s"},
    "ManufacturerInfo": {"icon": "mdi:factory"},
    "ControlFWBuildNo2": {"icon": "mdi:chip"},
    "ControlFWBuildNo1": {"icon": "mdi:chip"},
    "ComFWBuildNo2": {"icon": "mdi:chip"},
    "ComFWBuildNo1": {"icon": "mdi:chip"},
    "SysWeekly": {"icon": "mdi:calendar-week"},
    "ModbusVersion": {"icon": "mdi:protocol"},
    "SCCComMode": {"icon": "mdi:connection"},
    "ComboardVer": {"icon": "mdi:chip"},
    "uwBatPieceNum": {"icon": "mdi:battery-multiple"},
    "LiProtocolType": {"icon": "mdi:protocol"},
    "BLVersion2": {"icon": "mdi:chip"},
}

CONFIG_NUMBER_LIMITS = {
    "SysYear": {"min": 2000, "max": 2099, "step": 1},
    "SysMonth": {"min": 1, "max": 12, "step": 1},
    "SysDay": {"min": 1, "max": 31, "step": 1},
    "SysHour": {"min": 0, "max": 23, "step": 1},
    "SysMin": {"min": 0, "max": 59, "step": 1},
    "SysSec": {"min": 0, "max": 59, "step": 1},
}


class WriteQueueFullError(RuntimeError):
    """Raised when the pending Modbus write queue is full."""


class GrowattModbusClient:  # pylint: disable=too-many-instance-attributes
    """Modbus RTU client with bounded recovery around pymodbus operations."""

    MAX_READ_REGISTERS = 125
    # Growatt-compatible inverter firmwares can silently drop large reads even
    # though Modbus RTU permits up to 125 registers in one read response.
    MAX_DEVICE_READ_REGISTERS = 45
    MAX_WRITE_REGISTERS = 123

    def __init__(
        self,
        port: str,
        timeout_sec: float = 1.5,
        retries: int = 2,
        reconnect_delay_sec: float = 0.2,
    ):
        """
        Args:
            port (str): The serial port to use (e.g. "/dev/ttyUSB0").
            timeout_sec (float): Per-request serial timeout.
            retries (int): Number of application-level retries after a failed request.
            reconnect_delay_sec (float): Delay before reopening the serial port.
        """

        self._port = port
        self._timeout_sec = max(0.1, timeout_sec)
        self._retries = max(0, retries)
        self._reconnect_delay_sec = max(0.0, reconnect_delay_sec)
        self._client = ModbusClient(
            framer="rtu",
            port=port,
            baudrate=9600,
            stopbits=1,
            bytesize=8,
            parity="N",
            timeout=self._timeout_sec,
            retries=0,
        )
        self._consecutive_failures = 0
        self._next_allowed_operation: Optional[float] = None
        logger.info(
            "Modbus client configured port=%s timeout_sec=%s retries=%s "
            "reconnect_delay_sec=%s",
            self._port,
            self._timeout_sec,
            self._retries,
            self._reconnect_delay_sec,
        )

    def _connect_locked(self):
        """Open the serial client and fail if the port cannot be opened."""

        logger.info("Opening Modbus serial port port=%s", self._port)
        if not self._client.connect():
            logger.error("Unable to open Modbus serial port port=%s", self._port)
            raise ModbusException(f"Unable to open Modbus serial port {self._port}")
        logger.info("Modbus serial port opened port=%s", self._port)

    def _reopen_locked(self):
        """Reopen the serial port after a protocol or transport failure."""

        logger.warning("Reopening Modbus serial port port=%s", self._port)
        try:
            self._client.close()
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to close Modbus serial port: %s", exc)
        if self._reconnect_delay_sec:
            logger.debug(
                "Waiting before Modbus reconnect delay_sec=%s",
                self._reconnect_delay_sec,
            )
            sleep(self._reconnect_delay_sec)
        self._connect_locked()

    def _record_success_locked(self):
        """Clear the consecutive-failure counter after a valid response."""

        if self._consecutive_failures:
            logger.info(
                "Modbus request succeeded after failures failures=%s",
                self._consecutive_failures,
            )
        self._consecutive_failures = 0

    def _record_failure_locked(self, exc: Exception):
        """Track failures and reopen the serial port."""

        self._consecutive_failures += 1
        logger.warning(
            "Modbus request failed failures=%s error=%s",
            self._consecutive_failures,
            exc,
        )
        try:
            self._reopen_locked()
        except Exception as reopen_exc:  # pylint: disable=broad-except
            logger.warning(
                "Failed to reopen Modbus serial port: %s",
                reopen_exc,
            )

    def _with_recovery_locked(self, operation: Callable[[], Any]) -> Any:
        """Run a Modbus operation with bounded retries and recovery."""

        last_exception: Optional[Exception] = None
        for attempt in range(self._retries + 1):
            logger.debug(
                "Starting Modbus operation attempt=%s max_attempts=%s",
                attempt + 1,
                self._retries + 1,
            )
            try:
                result = operation()
                self._record_success_locked()
                logger.debug("Modbus operation completed attempt=%s", attempt + 1)
                return result
            except Exception as exc:  # pylint: disable=broad-except
                last_exception = exc
                self._record_failure_locked(exc)
                if attempt < self._retries and self._reconnect_delay_sec:
                    logger.debug(
                        "Waiting before Modbus retry attempt=%s delay_sec=%s",
                        attempt + 2,
                        self._reconnect_delay_sec,
                    )
                    sleep(self._reconnect_delay_sec)

        logger.error(
            "Modbus operation exhausted retries attempts=%s last_error=%s",
            self._retries + 1,
            last_exception,
        )
        raise ModbusException(
            f"Modbus operation failed after {self._retries + 1} attempt(s): "
            f"{last_exception}"
        )

    def defer_next_operations(self, delay_sec: float):
        """Require subsequent Modbus operations to wait for a settle interval."""

        delay_sec = max(0.0, delay_sec)
        deadline = perf_counter() + delay_sec
        if self._next_allowed_operation is None:
            self._next_allowed_operation = deadline
        else:
            self._next_allowed_operation = max(
                self._next_allowed_operation,
                deadline,
            )
        logger.debug("Deferred next Modbus operation delay_sec=%s", delay_sec)

    def wait_until_ready_for_operation(self):
        """Wait until a prior config write's settle interval has elapsed."""

        if self._next_allowed_operation is None:
            return

        wait_sec = self._next_allowed_operation - perf_counter()
        if wait_sec > 0.0:
            logger.debug(
                "Waiting after config write before Modbus operation wait_sec=%s",
                wait_sec,
            )
            sleep(wait_sec)
        self._next_allowed_operation = None

    @staticmethod
    def _validate_register_range(start: int, count: int):
        """Ensure a register range has valid addressing and length."""

        if start < 0:
            raise ValueError("Register start must be non-negative")
        if count <= 0:
            raise ValueError("Register count must be positive")

    def _validate_read_register_range(self, start: int, count: int):
        """Ensure a read request fits the inverter's reliable read size."""

        self._validate_register_range(start, count)
        if count > self.MAX_DEVICE_READ_REGISTERS:
            raise ValueError(
                f"Register count exceeds {self.MAX_DEVICE_READ_REGISTERS}-register "
                "inverter read maximum"
            )

    def _validate_write_register_range(self, start: int, count: int):
        """Ensure a function-16 write request fits in one Modbus frame."""

        self._validate_register_range(start, count)
        if count > self.MAX_WRITE_REGISTERS:
            raise ValueError(
                f"Register count exceeds {self.MAX_WRITE_REGISTERS}-register "
                "write maximum"
            )

    @staticmethod
    def _validate_register_values(values: List[int]):
        """Ensure all register words can be encoded in a Modbus frame."""

        for value in values:
            if not isinstance(value, int) or value < 0 or value > 0xFFFF:
                raise ValueError("Register values must be unsigned 16-bit integers")

    @staticmethod
    def _raise_for_modbus_error(response: Any):
        """Fail closed on Modbus exception responses."""

        if response is None:
            raise ModbusException("No response from Modbus device")
        if hasattr(response, "isError") and response.isError():
            raise ModbusException(str(response))

    @classmethod
    def _get_checked_registers(cls, response: Any, start: int, count: int) -> List[int]:
        """Validate a read response and return its registers."""

        cls._raise_for_modbus_error(response)
        registers = getattr(response, "registers", None)
        if registers is None:
            raise ModbusException("Modbus read response did not include registers")
        if len(registers) != count:
            raise ModbusException(
                f"Modbus read returned {len(registers)} registers for "
                f"{count} requested at {start}"
            )
        return list(registers)

    @classmethod
    def _check_write_response(cls, response: Any, start: int, count: int):
        """Validate a function-16 write acknowledgement."""

        cls._raise_for_modbus_error(response)
        address = getattr(response, "address", start)
        written_count = getattr(response, "count", count)
        if address != start or written_count != count:
            raise ModbusException(
                "Modbus write acknowledgement did not match requested address/count"
            )

    def _run_with_constraints(
        self, func: Callable[[int, int], Any], start: int, count: int
    ) -> List[int]:
        """Validate range, execute command, and check the response."""

        self._validate_read_register_range(start, count)
        logger.debug("Modbus read start=%s count=%s", start, count)
        response = func(start, count)
        registers = self._get_checked_registers(response, start, count)
        logger.debug(
            "Modbus read completed start=%s count=%s registers=%s",
            start,
            count,
            registers,
        )
        return registers

    def _run_write_with_constraints(
        self, func: Callable[[int, List[int]], Any], start: int, values: List[int]
    ):
        """Validate write range, execute command, and check the acknowledgement."""

        count = len(values)
        self._validate_write_register_range(start, count)
        self._validate_register_values(values)
        logger.debug("Modbus write start=%s count=%s values=%s", start, count, values)
        response = func(start, values)
        self._check_write_response(response, start, count)
        logger.debug("Modbus write acknowledged start=%s count=%s", start, count)
        return response

    def connect(self):
        """Connect to the Modbus server."""
        self._connect_locked()

    def close(self):
        """Close the connection to the Modbus server."""
        logger.info("Closing Modbus serial port port=%s", self._port)
        self._client.close()

    def read_input_registers(self, start: int, count: int):
        """Read input registers from the Modbus server."""
        logger.debug("Reading input registers start=%s count=%s", start, count)
        self.wait_until_ready_for_operation()
        return self._with_recovery_locked(
            lambda: self._run_with_constraints(
                self._client.read_input_registers, start, count
            )
        )

    def read_holding_registers(self, start: int, count: int = 1):
        """Read holding registers from the Modbus server."""
        logger.debug("Reading holding registers start=%s count=%s", start, count)
        self.wait_until_ready_for_operation()
        return self._with_recovery_locked(
            lambda: self._run_with_constraints(
                self._client.read_holding_registers, start, count
            )
        )

    def write_registers(self, address: int, values: List[int]):
        """Write multiple registers to the Modbus server."""
        logger.debug(
            "Writing holding registers start=%s count=%s values=%s",
            address,
            len(values),
            values,
        )
        self.wait_until_ready_for_operation()
        return self._with_recovery_locked(
            lambda: self._run_write_with_constraints(
                self._client.write_registers, address, values
            )
        )


def _build_register_windows(
    registers: Dict[str, tuple],
    max_window_registers: int = GrowattModbusClient.MAX_DEVICE_READ_REGISTERS,
) -> List[tuple[int, int]]:
    """Compute minimal read windows covering populated register ranges."""

    windows: List[List[int]] = []
    for start, length, *_ in sorted(registers.values(), key=lambda x: x[0]):
        end = start + length - 1
        if (
            windows
            and max(windows[-1][1], end) - min(windows[-1][0], start) + 1
            <= GrowattModbusClient.MAX_READ_REGISTERS
        ):
            windows[-1][0] = min(windows[-1][0], start)
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])

    split_windows: List[tuple[int, int]] = []
    for start, end in sorted(windows, key=lambda x: x[0]):
        current = start
        while current <= end:
            count = min(max_window_registers, end - current + 1)
            split_windows.append((current, count))
            current += count
    return split_windows


INPUT_REGISTER_WINDOWS = _build_register_windows(INPUT_REGISTERS)
HOLDING_REGISTER_WINDOWS = _build_register_windows(HOLDING_AND_WRITE_REGISTERS)


@dataclass
class ModbusAppConfig:  # pylint: disable=too-many-instance-attributes
    """Modbus runtime configuration."""

    port: str
    write_queue_size: int
    write_batch_delay_sec: float
    timeout_sec: float
    retries: int
    reconnect_delay_sec: float


class GrowattInverter:  # pylint: disable=too-many-instance-attributes
    """Class to interact with a Growatt inverter using Modbus RTU."""

    CONFIG_WRITE_SETTLE_DELAY_SEC = 0.85

    def __init__(self, config: ModbusAppConfig):
        """Initialize the Growatt inverter.

        Args:
            config (ModbusAppConfig): Modbus runtime configuration."""
        self.client = GrowattModbusClient(
            config.port,
            timeout_sec=config.timeout_sec,
            retries=config.retries,
            reconnect_delay_sec=config.reconnect_delay_sec,
        )
        self.write_batch_delay_sec = max(0.0, config.write_batch_delay_sec)
        self._write_queue: deque[tuple[int, List[int]]] = deque()
        self._write_queue_size = max(1, config.write_queue_size)
        self._next_write_flush: Optional[float] = None
        self._sync_time_interval_sec = 720.0
        self._next_sync_time: Optional[float] = None
        self._lock = RLock()
        logger.info(
            "Growatt inverter initialized write_queue_size=%s write_batch_delay_sec=%s",
            self._write_queue_size,
            self.write_batch_delay_sec,
        )

    def connect(self):
        """Connect to the Modbus server and schedule maintenance work."""
        logger.info("Connecting inverter")
        self.client.connect()
        self._next_sync_time = perf_counter()
        logger.info("Inverter connected; initial time sync scheduled")

    def close(self):
        """Close the connection to the Modbus server."""
        logger.info("Closing inverter")
        self.client.close()

    def sync_time(self) -> Optional[List[int]]:
        """Write the current local wall clock to the inverter."""

        with self._lock:
            self.client.wait_until_ready_for_operation()
            now = datetime.now()
            wait_sec = 1.0 - (now.microsecond / 1_000_000.0)
            if wait_sec > 0.0:
                logger.debug(
                    "Waiting for next second boundary before time sync wait_sec=%.6f",
                    wait_sec,
                )
                sleep(wait_sec)
                now = datetime.now()

            values = [
                now.year,  # SysYear (45)
                now.month,  # SysMonth (46)
                now.day,  # SysDay (47)
                now.hour,  # SysHour (48)
                now.minute,  # SysMin (49)
                now.second,  # SysSec (50)
            ]
            try:
                logger.info("Syncing inverter time values=%s", values)
                self.client.write_registers(
                    HOLDING_AND_WRITE_REGISTERS["SysYear"][0],  # 45
                    values,
                )
                logger.info("Inverter time sync completed values=%s", values)
                return values
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("Failed to update time: %s", exc)
                return None
            finally:
                self._next_sync_time = perf_counter() + self._sync_time_interval_sec
                logger.info(
                    "Next inverter time sync scheduled interval_sec=%s",
                    self._sync_time_interval_sec,
                )

    @staticmethod
    def _coalesce_writes(
        requested: Dict[int, List[int]],
    ) -> List[tuple[int, List[int]]]:
        """Merge adjacent pending writes into legal Modbus function-16 frames."""

        if not requested:
            return []

        words = {}
        for start, values in requested.items():
            for offset, value in enumerate(values):
                words[start + offset] = value

        batches: List[tuple[int, List[int]]] = []
        batch_start: Optional[int] = None
        batch_values: List[int] = []
        previous_address: Optional[int] = None

        for address, value in sorted(words.items()):
            can_extend = (
                batch_start is not None
                and previous_address is not None
                and address == previous_address + 1
                and len(batch_values) < GrowattModbusClient.MAX_WRITE_REGISTERS
            )
            if not can_extend:
                if batch_start is not None:
                    batches.append((batch_start, batch_values))
                batch_start = address
                batch_values = [value]
            else:
                batch_values.append(value)
            previous_address = address

        if batch_start is not None:
            batches.append((batch_start, batch_values))

        logger.debug(
            "Coalesced config writes requests=%s batches=%s",
            len(requested),
            len(batches),
        )
        return batches

    def flush_pending_writes(self):
        """Write queued register updates, coalescing nearby requests."""

        with self._lock:
            if not self._write_queue:
                self._next_write_flush = None
                logger.debug("No pending config writes to flush")
                return

            requested = {}
            while self._write_queue:
                start, values = self._write_queue.popleft()
                requested[start] = values
            self._next_write_flush = None
            batches = self._coalesce_writes(requested)
            failed_batches = 0

            for start, values in batches:
                try:
                    self.client.write_registers(start, values)
                    logger.debug(
                        "Config write flushed start=%s count=%s values=%s",
                        start,
                        len(values),
                        values,
                    )
                except Exception as exc:  # pylint: disable=broad-except
                    logger.error(
                        "Failed to write registers start=%s count=%s: %s",
                        start,
                        len(values),
                        exc,
                    )
                    failed_batches += 1
                finally:
                    self.client.defer_next_operations(
                        self.CONFIG_WRITE_SETTLE_DELAY_SEC
                    )

            if failed_batches:
                logger.warning(
                    "Config write flush completed with failures requests=%s batches=%s failed=%s",
                    len(requested),
                    len(batches),
                    failed_batches,
                )
            else:
                logger.info(
                    "Config writes flushed requests=%s batches=%s",
                    len(requested),
                    len(batches),
                )

    def run_maintenance(self):
        """Run scheduled inverter work."""

        with self._lock:
            now = perf_counter()
            if self._next_write_flush is not None and now >= self._next_write_flush:
                logger.debug("Maintenance flushing due config writes")
                self.flush_pending_writes()
            if self._next_sync_time is not None and now >= self._next_sync_time:
                logger.debug("Maintenance syncing inverter time")
                self.sync_time()

    def next_maintenance_timeout(self) -> Optional[float]:
        """Return seconds until the next scheduled inverter task."""

        with self._lock:
            now = perf_counter()
            timeouts = [
                deadline - now
                for deadline in (self._next_write_flush, self._next_sync_time)
                if deadline is not None
            ]
            if not timeouts:
                logger.debug("No scheduled maintenance timeout")
                return None
            timeout = max(0.0, min(timeouts))
            logger.debug("Next maintenance timeout timeout_sec=%s", timeout)
            return timeout

    def _read_register_windows(
        self, reader: Callable[[int, int], List[int]], windows: List[tuple[int, int]]
    ) -> List[int]:
        """Read populated register windows and place them in a single buffer."""

        if not windows:
            return []

        max_end = max(start + count for start, count in windows)
        registers: List[int] = [0] * max_end
        for start, count in windows:
            logger.debug("Reading register window start=%s count=%s", start, count)
            registers[start : start + count] = reader(start, count)

        return registers

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
            register.to_bytes(2, "big")
            for register in registers[start : start + length]
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
        return data.rstrip(b"\x00").decode("utf-8", errors="replace")

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

    @staticmethod
    def _postprocess_register_value(key: str, value: Any, postprocess: Callable):
        """Apply a register postprocessor and normalize malformed device values."""

        try:
            return postprocess(value)
        except (KeyError, ValueError, UnicodeDecodeError) as exc:
            raise ModbusException(
                f"Unexpected value for register {key}: {value!r}"
            ) from exc

    def read_status(self):
        """Read the system status and other information from the inverter."""
        with self._lock:
            logger.debug("Reading inverter status")
            reg = self._read_register_windows(
                self.client.read_input_registers, INPUT_REGISTER_WINDOWS
            )
            info = {}
            for key, value in INPUT_REGISTERS.items():
                start, length, type_, postprocess = value
                raw_value = self.generic_read_postprocess(reg, start, length, type_)
                info[key] = self._postprocess_register_value(
                    key, raw_value, postprocess
                )

            logger.debug("Completed inverter status read fields=%s", len(info))
            return info

    def read_config(self):
        """Read the system configuration from the inverter."""
        with self._lock:
            logger.debug("Reading inverter config")
            reg = self._read_register_windows(
                self.client.read_holding_registers, HOLDING_REGISTER_WINDOWS
            )
            info = {}
            for key, value in HOLDING_AND_WRITE_REGISTERS.items():
                start, length, type_, readpostprocess, _ = value
                raw_value = self.generic_read_postprocess(reg, start, length, type_)
                info[key] = self._postprocess_register_value(
                    key, raw_value, readpostprocess
                )

            logger.debug("Completed inverter config read fields=%s", len(info))
            return info

    def write_config(self, key: str, value: Union[str, int, float]):
        """Schedule a config write for the selector-loop maintenance hook.

        Args:
            key (str): The configuration key to write.
            value (Union[str, int, float]): The value to write."""
        with self._lock:
            try:
                start, length, type_, _, writepreprocess = HOLDING_AND_WRITE_REGISTERS[
                    key
                ]
            except KeyError as exc:
                logger.warning("Rejected config write with invalid key key=%s", key)
                raise KeyError("Invalid key") from exc
            if not writepreprocess:
                logger.warning("Rejected write to read-only config key=%s", key)
                raise ValueError("Register is not writeable")

            try:
                value = writepreprocess(value)
            except (ValueError, KeyError, OverflowError) as exc:
                logger.warning("Rejected config write with invalid value key=%s", key)
                raise ValueError("Invalid value") from exc

            match type_:
                case RegType.UINT | RegType.INT:
                    try:
                        values = self.uncombine_registers(
                            value, length, signed=type_ == RegType.INT
                        )
                    except OverflowError as exc:
                        raise ValueError("Invalid value") from exc
                case RegType.CHAR:
                    values = list(value.encode("utf-8"))
                    values = [
                        values[i] << 8 | values[i + 1]
                        if i + 1 < len(values)
                        else values[i]
                        for i in range(0, len(values), 2)
                    ]
                    values += [0] * (length - len(values))  # pad with zeros
                    if len(values) != length:
                        raise ValueError("Invalid value length")
                case _:
                    raise ValueError("Invalid register type")

            if len(self._write_queue) >= self._write_queue_size:
                logger.warning(
                    "Rejected config write because queue is full key=%s queue_size=%s",
                    key,
                    self._write_queue_size,
                )
                raise WriteQueueFullError("Write queue is full")

            self._write_queue.append((start, values))
            logger.debug(
                "Queued config write key=%s start=%s count=%s values=%s queue_depth=%s",
                key,
                start,
                len(values),
                values,
                len(self._write_queue),
            )
            if self._next_write_flush is None:
                self._next_write_flush = perf_counter() + self.write_batch_delay_sec
                logger.debug(
                    "Scheduled config write flush delay_sec=%s",
                    self.write_batch_delay_sec,
                )


@dataclass
class GrowattMqttConfig:  # pylint: disable=too-many-instance-attributes
    """MQTT and Home Assistant discovery runtime configuration."""

    host: str
    port: int
    username: Optional[str]
    password: Optional[str]
    client_id: str
    keepalive: int
    topic_prefix: str
    discovery_prefix: str
    device_id: str
    device_name: str
    retain: bool
    config_interval_sec: float

    def __post_init__(self):
        """Normalize timing and topic config."""

        self.keepalive = max(1, self.keepalive)
        self.config_interval_sec = max(1.0, self.config_interval_sec)
        self.topic_prefix = self.topic_prefix.strip("/") or self.device_id
        self.discovery_prefix = self.discovery_prefix.strip("/") or "homeassistant"


class GrowattMqttService:
    """Publish inverter state and accept config writes over MQTT."""

    STATUS_INTERVAL_SEC = 1.0
    STATUS_MAX_STALE_SEC = 6.0

    def __init__(self, inverter: GrowattInverter, config: GrowattMqttConfig):
        self.inverter = inverter
        self.config = config
        self._client = self._make_client()
        self._next_status_publish: Optional[float] = None
        self._next_config_publish: Optional[float] = None
        self._discovery_published = False

    @property
    def base_topic(self) -> str:
        """Return the normalized base topic."""

        return self.config.topic_prefix.strip("/")

    @property
    def availability_topic(self) -> str:
        """Return the MQTT availability topic."""

        return f"{self.base_topic}/availability"

    @staticmethod
    def _is_word_boundary(
        previous: str, char: str, next_char: str, has_current: bool = True
    ) -> bool:
        """Return whether an uppercase char starts a new register-name word."""

        if not has_current or not char.isupper():
            return False
        return (
            previous.islower()
            or previous.isdigit()
            or (previous.isupper() and next_char.islower())
        )

    @staticmethod
    def _slug(value: str) -> str:
        """Create a Home Assistant-safe object id fragment."""

        output = []
        previous_separator = False
        for index, char in enumerate(value):
            previous = value[index - 1] if index else ""
            next_char = value[index + 1] if index + 1 < len(value) else ""
            if (
                GrowattMqttService._is_word_boundary(
                    previous, char, next_char, bool(index)
                )
                and not previous_separator
            ):
                output.append("_")
            if char.isalnum():
                output.append(char.lower())
                previous_separator = False
            elif not previous_separator:
                output.append("_")
                previous_separator = True
        return "".join(output).strip("_")

    @staticmethod
    def _friendly_name(value: str) -> str:
        """Split a register key into a compact display name."""

        words = []
        current = ""
        for index, char in enumerate(value):
            previous = value[index - 1] if index else ""
            next_char = value[index + 1] if index + 1 < len(value) else ""
            if GrowattMqttService._is_word_boundary(
                previous, char, next_char, bool(current)
            ):
                words.append(current)
                current = char
            else:
                current += char
        if current:
            words.append(current)
        return " ".join(words)

    @classmethod
    def _value_topic(cls, base_topic: str, namespace: str, key: str) -> str:
        """Return a state topic for a register value."""

        return f"{base_topic}/{namespace}/{cls._slug(key)}/state"

    @staticmethod
    def _mqtt_value(value: Any) -> str:
        """Serialize a scalar value for MQTT state and command topics."""

        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    @staticmethod
    def _sensor_metadata(key: str) -> Dict[str, str]:
        """Infer Home Assistant sensor metadata from the register name."""

        metadata: Dict[str, str] = dict(MQTT_ENTITY_METADATA.get(key, {}))
        lower_key = key.lower()
        for matches, inferred_metadata in (
            (
                (
                    "seconds",
                    lambda value: (
                        "time" in value and ("volt" in value or "freq" in value)
                    ),
                ),
                {
                    "device_class": "duration",
                    "icon": "mdi:timer-outline",
                    "unit_of_measurement": "s",
                },
            ),
            (
                (lambda value: value.endswith("kwh"),),
                {
                    "device_class": "energy",
                    "icon": "mdi:lightning-bolt",
                    "unit_of_measurement": "kWh",
                    "state_class": "total_increasing",
                },
            ),
            (
                (lambda value: value.endswith("kw"),),
                {
                    "device_class": "power",
                    "icon": "mdi:flash",
                    "unit_of_measurement": "kW",
                },
            ),
            (
                ("percent", lambda value: value.endswith("soc")),
                {
                    "icon": "mdi:percent-outline",
                    "unit_of_measurement": "%",
                },
            ),
            (
                (lambda value: "temp" in value and value.endswith("c"),),
                {
                    "device_class": "temperature",
                    "icon": "mdi:thermometer",
                    "unit_of_measurement": "°C",
                },
            ),
            (
                ("watt",),
                {
                    "device_class": "power",
                    "icon": "mdi:flash",
                    "unit_of_measurement": "W",
                },
            ),
            (
                ("volt",),
                {
                    "device_class": "voltage",
                    "icon": "mdi:sine-wave",
                    "unit_of_measurement": "V",
                },
            ),
            (
                (lambda value: value.endswith("va"),),
                {
                    "device_class": "apparent_power",
                    "icon": "mdi:flash-triangle-outline",
                    "unit_of_measurement": "VA",
                },
            ),
            (
                ("amps",),
                {
                    "device_class": "current",
                    "icon": "mdi:current-ac",
                    "unit_of_measurement": "A",
                },
            ),
            (
                ("freq",),
                {
                    "device_class": "frequency",
                    "icon": "mdi:sine-wave",
                    "unit_of_measurement": "Hz",
                },
            ),
        ):
            if any(
                match(lower_key) if callable(match) else match in lower_key
                for match in matches
            ):
                metadata.update(inferred_metadata)
                break

        for matches, icon in (
            (("fan",), "mdi:fan"),
            (("battery", lambda value: value.startswith("bat")), "mdi:battery"),
            (("pv",), "mdi:solar-panel"),
            (("grid", "uti"), "mdi:transmission-tower"),
            (("output",), "mdi:power-plug-outline"),
            (("buzzer", "alarm"), "mdi:bell-ring-outline"),
            (("restart", "reset"), "mdi:restart"),
            (("enable",), "mdi:toggle-switch-outline"),
        ):
            if "icon" in metadata:
                break
            if any(
                match(lower_key) if callable(match) else match in lower_key
                for match in matches
            ):
                metadata["icon"] = icon

        if (
            "unit_of_measurement" in metadata
            and "state_class" not in metadata
            and metadata.get("device_class") != "duration"
        ):
            metadata["state_class"] = "measurement"
        return metadata

    def _make_client(self):
        """Create a paho client compatible with paho-mqtt 1.x and 2.x."""

        try:
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=self.config.client_id,
            )
        except AttributeError:
            client = mqtt.Client(client_id=self.config.client_id)

        if self.config.username is not None:
            client.username_pw_set(self.config.username, self.config.password)
        client.will_set(self.availability_topic, "offline", retain=True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        return client

    def start(self):
        """Connect to MQTT and start the broker network loop."""

        logger.info(
            "Connecting MQTT broker host=%s port=%s client_id=%s",
            self.config.host,
            self.config.port,
            self.config.client_id,
        )
        self._client.connect(self.config.host, self.config.port, self.config.keepalive)
        self._client.loop_start()
        self._next_status_publish = perf_counter()
        self._next_config_publish = perf_counter()

    def stop(self):
        """Publish offline availability and close the MQTT client."""

        try:
            self._client.publish(self.availability_topic, "offline", retain=True)
            self._client.loop_stop()
            self._client.disconnect()
            logger.info("MQTT client stopped")
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Failed to stop MQTT client: %s", exc)

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties=None):
        """Handle MQTT connection and subscribe to command topics."""

        try:
            connected = int(reason_code) == 0
        except TypeError:
            connected = str(reason_code).lower() == "success"
        if not connected:
            logger.error("MQTT connection failed reason=%s", reason_code)
            return
        logger.info("MQTT connected")
        client.publish(self.availability_topic, "online", retain=True)
        client.subscribe(f"{self.base_topic}/config/+/set")
        client.subscribe(f"{self.base_topic}/time_sync/set")
        self._publish_discovery()
        self._discovery_published = True
        self._next_status_publish = perf_counter()
        self._next_config_publish = perf_counter()

    def _on_disconnect(
        self,
        _client,
        _userdata,
        _disconnect_flags=None,
        reason_code=None,
        _properties=None,
    ):
        """Log MQTT disconnections."""

        logger.warning("MQTT disconnected reason=%s", reason_code)

    def _on_message(self, _client, _userdata, message):
        """Handle Home Assistant command topics."""

        topic = message.topic
        payload = message.payload.decode("utf-8", errors="replace").strip()
        logger.debug("MQTT command received topic=%s", topic)
        if topic == f"{self.base_topic}/time_sync/set":
            self.inverter.sync_time()
            self._next_config_publish = perf_counter()
            return

        prefix = f"{self.base_topic}/config/"
        suffix = "/set"
        if not topic.startswith(prefix) or not topic.endswith(suffix):
            return
        key_slug = topic[len(prefix) : -len(suffix)]
        key = next(
            (
                name
                for name in HOLDING_AND_WRITE_REGISTERS
                if self._slug(name) == key_slug
            ),
            None,
        )
        if key is None:
            logger.warning(
                "Ignoring MQTT command for unknown config key topic=%s", topic
            )
            return

        try:
            value = self._parse_command_payload(payload)
            self.inverter.write_config(key, value)
            write_delay_sec = getattr(self.inverter, "write_batch_delay_sec", 0.0)
            if not isinstance(write_delay_sec, (int, float)):
                write_delay_sec = 0.0
            self._next_config_publish = perf_counter() + max(
                1.0,
                write_delay_sec + GrowattInverter.CONFIG_WRITE_SETTLE_DELAY_SEC,
            )
            logger.info("Accepted MQTT config command key=%s", key)
        except (KeyError, ValueError, WriteQueueFullError, ModbusException) as exc:
            logger.warning("Rejected MQTT config command key=%s error=%s", key, exc)

    @staticmethod
    def _parse_command_payload(payload: str) -> Union[str, int, float, bool]:
        """Parse a command payload from HA text, switch, select, or number entities."""

        if payload.lower() in ("true", "false", "on", "off"):
            return payload.lower() in ("true", "on")
        try:
            decoded = json_loads(payload)
            if isinstance(decoded, (str, int, float, bool)):
                return decoded
        except ValueError:
            pass
        return parse_config_value(payload)

    def _device_payload(self) -> Dict[str, Any]:
        """Return the shared Home Assistant device block."""

        return {
            "identifiers": [self.config.device_id],
            "name": self.config.device_name,
            "manufacturer": "Growatt",
            "model": "SPF 5000 ES",
        }

    def _entity_base_payload(self, object_id: str, name: str) -> Dict[str, Any]:
        """Return fields common to all MQTT discovery entities."""

        return {
            "name": name,
            "object_id": object_id,
            "unique_id": f"{self.config.device_id}_{object_id}",
            "availability_topic": self.availability_topic,
            "device": self._device_payload(),
        }

    def _publish_discovery(self):
        """Publish retained Home Assistant discovery configuration."""

        logger.info("Publishing Home Assistant MQTT discovery")
        for key in INPUT_REGISTERS:
            object_id = f"{self.config.device_id}_{self._slug(key)}"
            payload = self._entity_base_payload(object_id, self._friendly_name(key))
            payload.update(
                {
                    "state_topic": self._value_topic(self.base_topic, "status", key),
                }
            )
            payload.update(self._sensor_metadata(key))
            self._publish_discovery_payload("sensor", object_id, payload)

        for key, item in HOLDING_AND_WRITE_REGISTERS.items():
            component, payload = self._config_entity_discovery_payload(key, item)
            object_id = payload["object_id"]
            self._publish_discovery_payload(component, object_id, payload)
            if component != "sensor":
                self._clear_discovery_payload("sensor", object_id)

        object_id = f"{self.config.device_id}_sync_time"
        payload = self._entity_base_payload(object_id, "Sync Time")
        payload["command_topic"] = f"{self.base_topic}/time_sync/set"
        payload["payload_press"] = "sync"
        payload["icon"] = "mdi:clock-sync-outline"
        self._publish_discovery_payload("button", object_id, payload)

    def _config_entity_discovery_payload(
        self, key: str, item: tuple
    ) -> tuple[str, Dict[str, Any]]:
        """Build discovery payload for a config register."""

        _, _, type_, readpreprocess, writepreprocess = item
        writable = writepreprocess is not None
        object_id = f"{self.config.device_id}_{self._slug(key)}"
        payload = self._entity_base_payload(object_id, self._friendly_name(key))
        payload["state_topic"] = self._value_topic(self.base_topic, "config", key)
        if writable:
            payload["command_topic"] = f"{self.base_topic}/config/{self._slug(key)}/set"

        if readpreprocess is bool:
            payload.update(
                {
                    "payload_on": "true",
                    "payload_off": "false",
                    "icon": "mdi:toggle-switch-outline",
                }
            )
            if not writable:
                component = "binary_sensor"
            else:
                payload.update(
                    {
                        "state_on": "true",
                        "state_off": "false",
                    }
                )
                component = "switch"
        elif not writable:
            payload.update(self._sensor_metadata(key))
            component = "sensor"
        elif key in CONFIG_SELECT_OPTIONS:
            payload.update(self._sensor_metadata(key))
            payload["options"] = CONFIG_SELECT_OPTIONS[key]
            component = "select"
        elif key in CONFIG_BOOLEAN_KEYS:
            payload.update(self._sensor_metadata(key))
            payload.update(
                {
                    "payload_on": "true",
                    "payload_off": "false",
                    "state_on": "true",
                    "state_off": "false",
                }
            )
            component = "switch"
        elif type_ == RegType.CHAR:
            payload.update(self._sensor_metadata(key))
            component = "text"
        else:
            payload.update(self._sensor_metadata(key))
            payload.update(
                CONFIG_NUMBER_LIMITS.get(key, {"min": 0, "max": 65535, "step": 1})
            )
            payload["mode"] = "box"
            component = "number"

        return component, payload

    def _publish_discovery_payload(
        self, component: str, object_id: str, payload: Dict[str, Any]
    ):
        """Publish one Home Assistant MQTT discovery config payload."""

        topic = (
            f"{self.config.discovery_prefix.strip('/')}/{component}/"
            f"{self.config.device_id}/{object_id}/config"
        )
        self._client.publish(topic, json_dumps(payload), retain=True)

    def _clear_discovery_payload(self, component: str, object_id: str):
        """Clear a retained Home Assistant discovery config payload."""

        topic = (
            f"{self.config.discovery_prefix.strip('/')}/{component}/"
            f"{self.config.device_id}/{object_id}/config"
        )
        self._client.publish(topic, "", retain=True)

    def run_maintenance(self):
        """Publish scheduled status and config states."""

        now = perf_counter()
        if not self._discovery_published:
            return

        status_due = (
            self._next_status_publish is not None and now >= self._next_status_publish
        )
        config_due = (
            self._next_config_publish is not None and now >= self._next_config_publish
        )
        status_stale = (
            self._next_status_publish is not None
            and now >= self._next_status_publish + self.STATUS_MAX_STALE_SEC
        )

        if status_due and (not config_due or status_stale):
            self.publish_status()
            self._next_status_publish = now + self.STATUS_INTERVAL_SEC
            status_due = False

        if config_due:
            self.publish_config()
            self._next_config_publish = now + self.config.config_interval_sec

        if status_due:
            self.publish_status()
            self._next_status_publish = now + self.STATUS_INTERVAL_SEC

    def is_status_publish_stale(self) -> bool:
        """Return whether status has reached its maximum allowed staleness."""

        if not self._discovery_published or self._next_status_publish is None:
            return False
        return perf_counter() >= self._next_status_publish + self.STATUS_MAX_STALE_SEC

    def next_maintenance_timeout(self) -> Optional[float]:
        """Return seconds until the next MQTT publish is due."""

        if not self._discovery_published:
            return None
        now = perf_counter()
        deadlines = [
            deadline - now
            for deadline in (self._next_status_publish, self._next_config_publish)
            if deadline is not None
        ]
        if not deadlines:
            return None
        return max(0.0, min(deadlines))

    def publish_status(self):
        """Read and publish status registers."""

        try:
            status = self.inverter.read_status()
        except ModbusException as exc:
            logger.error("MQTT status publish failed: %s", exc)
            return
        for key, value in status.items():
            self._client.publish(
                self._value_topic(self.base_topic, "status", key),
                self._mqtt_value(value),
                retain=self.config.retain,
            )
        logger.debug("MQTT status published fields=%s", len(status))

    def publish_config(self):
        """Read and publish config registers."""

        try:
            config = self.inverter.read_config()
        except ModbusException as exc:
            logger.error("MQTT config publish failed: %s", exc)
            return
        for key, value in config.items():
            self._client.publish(
                self._value_topic(self.base_topic, "config", key),
                self._mqtt_value(value),
                retain=self.config.retain,
            )
        logger.debug("MQTT config published fields=%s", len(config))


@dataclass
class GrowattAppConfig:
    """Application runtime configuration."""

    modbus: ModbusAppConfig
    mqtt: GrowattMqttConfig
    log_level: str


def configure_logging(log_level: str):
    """Configure process logging."""

    level_name = log_level.strip().upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        raise ValueError(f"Invalid LOG_LEVEL {log_level!r}")
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info("Logging configured level=%s", level_name)


def read_app_config(config_path: str = "config.ini") -> GrowattAppConfig:
    """Read application configuration from an INI file."""

    cfg = configparser.ConfigParser()
    cfg.read(config_path)
    logger.debug("Read application config path=%s", config_path)
    mqtt_device_id = cfg.get("MQTT", "DEVICE_ID", fallback="growatt_spf5000es")
    return GrowattAppConfig(
        modbus=ModbusAppConfig(
            port=cfg.get("MODBUS", "PORT"),
            write_queue_size=cfg.getint("MODBUS", "WRITE_QUEUE_SIZE", fallback=128),
            write_batch_delay_sec=cfg.getfloat(
                "MODBUS", "WRITE_BATCH_DELAY_SEC", fallback=0.25
            ),
            timeout_sec=cfg.getfloat("MODBUS", "TIMEOUT_SEC", fallback=1.5),
            retries=cfg.getint("MODBUS", "RETRIES", fallback=2),
            reconnect_delay_sec=cfg.getfloat(
                "MODBUS", "RECONNECT_DELAY_SEC", fallback=0.2
            ),
        ),
        mqtt=GrowattMqttConfig(
            host=cfg.get("MQTT", "HOST", fallback="localhost"),
            port=cfg.getint("MQTT", "PORT", fallback=1883),
            username=optional_str(cfg.get("MQTT", "USER", fallback=None)),
            password=optional_str(cfg.get("MQTT", "PASSWORD", fallback=None)),
            client_id=cfg.get("MQTT", "CLIENT_ID", fallback=mqtt_device_id),
            keepalive=cfg.getint("MQTT", "KEEPALIVE_SEC", fallback=60),
            topic_prefix=cfg.get("MQTT", "TOPIC_PREFIX", fallback=mqtt_device_id),
            discovery_prefix=cfg.get(
                "MQTT", "DISCOVERY_PREFIX", fallback="homeassistant"
            ),
            device_id=mqtt_device_id,
            device_name=cfg.get("MQTT", "DEVICE_NAME", fallback="Growatt SPF 5000 ES"),
            retain=cfg.getboolean("MQTT", "RETAIN", fallback=True),
            config_interval_sec=cfg.getfloat(
                "MQTT", "CONFIG_INTERVAL_SEC", fallback=1800.0
            ),
        ),
        log_level=cfg.get("LOGGING", "LEVEL", fallback="INFO"),
    )


def main():
    """Main function."""
    inverter: Optional[GrowattInverter] = None
    mqtt_service: Optional[GrowattMqttService] = None
    try:
        app_config = read_app_config()
        configure_logging(app_config.log_level)

        logger.info("Inverter port set to %s", app_config.modbus.port)
        logger.info(
            "MQTT broker=%s:%s topic_prefix=%s discovery_prefix=%s",
            app_config.mqtt.host,
            app_config.mqtt.port,
            app_config.mqtt.topic_prefix,
            app_config.mqtt.discovery_prefix,
        )
        inverter = GrowattInverter(app_config.modbus)
        mqtt_service = GrowattMqttService(inverter, app_config.mqtt)
        inverter.connect()
        mqtt_service.start()

        def run_maintenance():
            if mqtt_service and mqtt_service.is_status_publish_stale():
                mqtt_service.run_maintenance()
            inverter.run_maintenance()
            if mqtt_service:
                mqtt_service.run_maintenance()

        def next_maintenance_timeout():
            timeouts = [
                timeout
                for timeout in (
                    inverter.next_maintenance_timeout(),
                    mqtt_service.next_maintenance_timeout() if mqtt_service else None,
                )
                if timeout is not None
            ]
            return min(timeouts) if timeouts else None

        logger.info("MQTT service loop started")
        while True:
            run_maintenance()
            timeout = next_maintenance_timeout()
            sleep(min(0.5, timeout) if timeout is not None else 0.5)
    except KeyboardInterrupt:
        logger.info("Interrupted; shutting down")
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("%s", exc)
    finally:
        try:
            if mqtt_service:
                mqtt_service.stop()
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Failed to close MQTT client: %s", exc)

        try:
            if inverter:
                inverter.close()
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Failed to close inverter: %s", exc)


if __name__ == "__main__":
    main()
