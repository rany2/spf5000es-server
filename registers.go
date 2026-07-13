package main

import (
	"encoding/binary"
	"fmt"
	"math"
	"strconv"
	"strings"
	"unicode/utf8"
)

type registerKind uint8

const (
	regUint registerKind = iota
	regInt
	regChar
)

type registerDef struct {
	Start, Length int
	Kind          registerKind
	Decode        func(any) (any, error)
	Encode        func(any) (any, error)
}

func identity(v any) (any, error) { return v, nil }
func asInt(v any) (any, error) {
	switch x := v.(type) {
	case int64:
		return int(x), nil
	case int:
		return x, nil
	default:
		return nil, fmt.Errorf("integer expected")
	}
}
func scaled(div float64) func(any) (any, error) {
	return func(v any) (any, error) {
		x, ok := v.(int64)
		if !ok {
			return nil, fmt.Errorf("integer expected")
		}
		return float64(x) / div, nil
	}
}
func scaleEncode(mult float64) func(any) (any, error) {
	return func(v any) (any, error) {
		f, e := number(v)
		if e != nil {
			return nil, e
		}
		return int64(math.Round(f * mult)), nil
	}
}
func boolDecode(v any) (any, error) {
	x, ok := v.(int64)
	if !ok {
		return nil, fmt.Errorf("integer expected")
	}
	return x != 0, nil
}
func boolEncode(v any) (any, error) {
	b, e := toBool(v)
	if e != nil {
		return nil, e
	}
	if b {
		return int64(1), nil
	}
	return int64(0), nil
}
func stringEncode(v any) (any, error) {
	s, ok := v.(string)
	if !ok {
		return nil, fmt.Errorf("string expected")
	}
	return s, nil
}
func integerEncode(v any) (any, error) {
	f, e := number(v)
	if e != nil {
		return nil, e
	}
	if math.Trunc(f) != f {
		return nil, fmt.Errorf("integer expected")
	}
	return int64(f), nil
}

func number(v any) (float64, error) {
	switch x := v.(type) {
	case int:
		return float64(x), nil
	case int64:
		return float64(x), nil
	case float64:
		if math.IsInf(x, 0) || math.IsNaN(x) {
			return 0, fmt.Errorf("finite number expected")
		}
		return x, nil
	case string:
		f, e := strconv.ParseFloat(x, 64)
		if e != nil || math.IsInf(f, 0) || math.IsNaN(f) {
			return 0, fmt.Errorf("number expected")
		}
		return f, nil
	default:
		return 0, fmt.Errorf("number expected")
	}
}
func toBool(v any) (bool, error) {
	switch x := v.(type) {
	case bool:
		return x, nil
	case int:
		return x != 0, nil
	case int64:
		return x != 0, nil
	case string:
		switch strings.ToLower(x) {
		case "yes", "true", "t", "1", "on":
			return true, nil
		case "no", "false", "f", "0", "off":
			return false, nil
		}
	}
	return false, fmt.Errorf("boolean expected")
}
func enumDecoder(values map[int64]any, fallback bool) func(any) (any, error) {
	return func(v any) (any, error) {
		x, ok := v.(int64)
		if !ok {
			return nil, fmt.Errorf("integer expected")
		}
		if out, ok := values[x]; ok {
			return out, nil
		}
		if fallback {
			return fmt.Sprintf("Unknown (%d)", x), nil
		}
		return nil, fmt.Errorf("unknown enum value %d", x)
	}
}
func enumEncoder(values map[string]int64) func(any) (any, error) {
	return func(v any) (any, error) {
		s, ok := v.(string)
		if !ok {
			return nil, fmt.Errorf("string expected")
		}
		x, ok := values[s]
		if !ok {
			return nil, fmt.Errorf("unknown enum value")
		}
		return x, nil
	}
}

func r(start, length int, kind registerKind, decode func(any) (any, error), encode func(any) (any, error)) registerDef {
	return registerDef{start, length, kind, decode, encode}
}

var systemStatus = map[int64]any{0: "Standby", 1: "PV&Grid Supporting Loads", 2: "Battery Discharging", 3: "Fault", 4: "Flash", 5: "PV Charging", 6: "Grid Charging", 7: "PV&Grid Charging", 8: "PV&Grid Charging+Grid Bypass", 9: "PV Charging+Grid Bypass", 10: "Grid Charging+Grid Bypass", 11: "Grid Bypass", 12: "PV Charging+Loads Supporting", 13: "PV Discharging", 14: "PV&Battery Discharging", 15: "Gen Charging", 16: "Gen Charging+Gen Bypass", 17: "PV&Gen Charging", 18: "PV&Gen Charging+Gen Bypass", 19: "PV Charging+Gen Bypass", 20: "Gen Bypass", 21: "PV Export to Grid", 22: "PV Export to Grid+Loads Supporting", 23: "PV Charging+Export to Grid", 24: "PV Charging+Export to Grid+Loads Supporting", 25: "Battery Export to Grid", 26: "Battery Export to Grid+Loads Supporting", 27: "Battery&PV Export to Grid", 28: "Battery&PV Export to Grid+Loads Supporting"}

var inputRegisters = map[string]registerDef{
	"SystemStatus": r(0, 1, regUint, enumDecoder(systemStatus, true), nil),
	"PV1Volt":      r(1, 1, regUint, scaled(10), nil), "PV2Volt": r(2, 1, regUint, scaled(10), nil),
	"PV1Watt": r(3, 2, regUint, scaled(10), nil), "PV2Watt": r(5, 2, regUint, scaled(10), nil),
	"PV1Amps": r(7, 1, regUint, scaled(10), nil), "PV2Amps": r(8, 1, regUint, scaled(10), nil),
	"OutputWatt": r(9, 2, regUint, scaled(10), nil), "OutputVA": r(11, 2, regUint, scaled(10), nil),
	"ACChrWatt": r(13, 2, regUint, scaled(10), nil), "ACChrVA": r(15, 2, regUint, scaled(10), nil),
	"BatteryVolt": r(17, 1, regUint, scaled(100), nil), "BatterySOC": r(18, 1, regUint, asInt, nil),
	"BusVolt": r(19, 1, regUint, scaled(10), nil), "GridVolt": r(20, 1, regUint, scaled(10), nil),
	"LineFreq": r(21, 1, regUint, scaled(100), nil), "OutputACVolt": r(22, 1, regUint, scaled(10), nil),
	"OutputACFreq": r(23, 1, regUint, scaled(100), nil), "OutputDCVolt": r(24, 1, regUint, scaled(10), nil),
	"InvTempC": r(25, 1, regInt, scaled(10), nil), "DCDCTempC": r(26, 1, regInt, scaled(10), nil),
	"LoadPercent": r(27, 1, regUint, scaled(10), nil), "BatteryPortVolt": r(28, 1, regUint, scaled(100), nil),
	"BatteryBusVolt": r(29, 1, regUint, scaled(100), nil), "WorkTimeTotalSeconds": r(30, 2, regUint, scaled(2), nil),
	"Buck1TempC": r(32, 1, regInt, scaled(10), nil), "Buck2TempC": r(33, 1, regInt, scaled(10), nil),
	"OutputAmps": r(34, 1, regUint, scaled(10), nil), "InvAmps": r(35, 1, regUint, scaled(10), nil),
	"ACInputWatt": r(36, 2, regInt, scaled(10), nil), "ACInputVA": r(38, 2, regUint, scaled(10), nil),
	"FaultBit": r(40, 1, regUint, asInt, nil), "WarningBit": r(41, 1, regUint, asInt, nil), "WarningBitHigh": r(42, 1, regUint, asInt, nil), "WarningValue": r(43, 1, regUint, asInt, nil), "DeviceTypeCode": r(44, 1, regUint, asInt, nil),
	"ExportToGridTodaykWh": r(45, 1, regUint, scaled(10), nil), "ExportToGridTotalkWh": r(46, 2, regUint, scaled(10), nil),
	"PV1EnergyTodaykWh": r(48, 2, regUint, scaled(10), nil), "PV1EnergyTotalkWh": r(50, 2, regUint, scaled(10), nil),
	"PV2EnergyTodaykWh": r(52, 2, regUint, scaled(10), nil), "PV2EnergyTotalkWh": r(54, 2, regUint, scaled(10), nil),
	"ACChargeEnergyTodaykWh": r(56, 2, regUint, scaled(10), nil), "ACChargeEnergyTotalkWh": r(58, 2, regUint, scaled(10), nil),
	"BatteryDischargeEnergyTodaykWh": r(60, 2, regUint, scaled(10), nil), "BatteryDischargeEnergyTotalkWh": r(62, 2, regUint, scaled(10), nil),
	"ACDischargeEnergyTodaykWh": r(64, 2, regUint, scaled(10), nil), "ACDischargeEnergyTotalkWh": r(66, 2, regUint, scaled(10), nil),
	"ACChargeBatteryAmps": r(68, 1, regUint, scaled(10), nil), "ACDischargeWatt": r(69, 2, regUint, scaled(10), nil), "ACDischargeVA": r(71, 2, regUint, scaled(10), nil),
	"BatteryDischargeWatt": r(73, 2, regUint, scaled(10), nil), "BatteryDischargeVA": r(75, 2, regUint, scaled(10), nil), "BatteryWatt": r(77, 2, regInt, scaled(10), nil),
	"SlaveExistCount": r(79, 1, regUint, asInt, nil), "MpptFanSpeedPercent": r(81, 1, regUint, asInt, nil), "InvFanSpeedPercent": r(82, 1, regUint, asInt, nil),
	"TotalChargeAmps": r(83, 1, regUint, scaled(10), nil), "TotalDischargeAmps": r(84, 1, regUint, scaled(10), nil), "OPDischargeEnergyTodaykWh": r(85, 2, regUint, scaled(10), nil), "OPDischargeEnergyTotalkWh": r(87, 2, regUint, scaled(10), nil),
}

var (
	outputConfigR = map[int64]any{0: "SBU", 1: "SOL", 2: "UTI", 3: "SUB"}
	outputConfigW = map[string]int64{"SBU": 0, "SOL": 1, "UTI": 2, "SUB": 3}
	chargeConfigR = map[int64]any{0: "PV First", 1: "PV&UTI", 2: "PV Only"}
	chargeConfigW = map[string]int64{"PV First": 0, "PV&UTI": 1, "PV Only": 2}
	pvModelR      = map[int64]any{0: "Independent", 1: "Parallel"}
	pvModelW      = map[string]int64{"Independent": 0, "Parallel": 1}
	acInModelR    = map[int64]any{0: "APL", 1: "UPS", 2: "GEN"}
	acInModelW    = map[string]int64{"APL": 0, "UPS": 1, "GEN": 2}
	outputVoltR   = map[int64]any{0: "208VAC", 1: "230VAC", 2: "240VAC", 3: "220VAC", 4: "100VAC", 5: "110VAC", 6: "120VAC"}
	outputVoltW   = map[string]int64{"208VAC": 0, "230VAC": 1, "240VAC": 2, "220VAC": 3, "100VAC": 4, "110VAC": 5, "120VAC": 6}
	outputFreqR   = map[int64]any{0: "50Hz", 1: "60Hz"}
	outputFreqW   = map[string]int64{"50Hz": 0, "60Hz": 1}
	overloadR     = map[int64]any{0: "Yes", 1: "No", 2: "Switch to UTI"}
	overloadW     = map[string]int64{"Yes": 0, "No": 1, "Switch to UTI": 2}
	batteryTypeR  = map[int64]any{0: "AGM", 1: "FLD", 2: "USE", 3: "Lithium", 4: "USE2"}
	batteryTypeW  = map[string]int64{"AGM": 0, "FLD": 1, "USE": 2, "Lithium": 3, "USE2": 4}
	agingR        = map[int64]any{0: "Normal", 1: "Aging"}
	agingW        = map[string]int64{"Normal": 0, "Aging": 1}
	safetyR       = map[int64]any{1: "Standard", 2: "ETL", 3: "AS4777", 4: "CQC", 5: "VDE4105"}
	safetyW       = map[string]int64{"Standard": 1, "ETL": 2, "AS4777": 3, "CQC": 4, "VDE4105": 5}
)

func overTempDecode(v any) (any, error) {
	x, ok := v.(int64)
	if !ok {
		return nil, fmt.Errorf("integer expected")
	}
	if x == 0 {
		return true, nil
	}
	if x == 1 {
		return false, nil
	}
	return nil, fmt.Errorf("unknown enum")
}
func overTempEncode(v any) (any, error) {
	b, e := toBool(v)
	if e != nil {
		return nil, e
	}
	if b {
		return int64(0), nil
	}
	return int64(1), nil
}

var holdingRegisters = map[string]registerDef{
	"OnOff":        r(0, 1, regUint, enumDecoder(map[int64]any{0: "Output enable", 0x100: "Output disable"}, false), nil),
	"OutputConfig": r(1, 1, regUint, enumDecoder(outputConfigR, false), enumEncoder(outputConfigW)), "ChargeConfig": r(2, 1, regUint, enumDecoder(chargeConfigR, false), enumEncoder(chargeConfigW)),
	"UtiOutStart": r(3, 1, regUint, asInt, integerEncode), "UtiOutEnd": r(4, 1, regUint, asInt, integerEncode), "UtiChargeStart": r(5, 1, regUint, asInt, integerEncode), "UtiChargeEnd": r(6, 1, regUint, asInt, integerEncode),
	"PVModel": r(7, 1, regUint, enumDecoder(pvModelR, false), enumEncoder(pvModelW)), "ACInModel": r(8, 1, regUint, enumDecoder(acInModelR, false), enumEncoder(acInModelW)),
	"FWVersion": r(9, 3, regChar, identity, nil), "FWVersion2": r(12, 3, regChar, identity, nil), "LCDLanguage": r(15, 1, regUint, asInt, integerEncode), "GridV_Adj": r(16, 1, regUint, asInt, nil), "InvV_Adj": r(17, 1, regUint, asInt, nil),
	"OutputVoltType": r(18, 1, regUint, enumDecoder(outputVoltR, false), enumEncoder(outputVoltW)), "OutputFreqType": r(19, 1, regUint, enumDecoder(outputFreqR, false), enumEncoder(outputFreqW)), "OverLoadRestart": r(20, 1, regUint, enumDecoder(overloadR, false), enumEncoder(overloadW)),
	"OverTempRestart": r(21, 1, regUint, overTempDecode, overTempEncode), "BuzzerEnable": r(22, 1, regUint, boolDecode, boolEncode), "SerialNumber": r(23, 5, regChar, identity, stringEncode),
	"MoudleH": r(28, 1, regUint, asInt, integerEncode), "MoudleL": r(29, 1, regUint, asInt, integerEncode), "ComAddress": r(30, 1, regUint, asInt, integerEncode), "FlashStart": r(31, 1, regUint, asInt, integerEncode), "ResetUserInfo": r(32, 1, regUint, asInt, integerEncode), "ResetToFactory": r(33, 1, regUint, asInt, integerEncode), "MaxChargeAmps": r(34, 1, regUint, asInt, integerEncode),
	"BulkChargeVolt": r(35, 1, regUint, scaled(10), scaleEncode(10)), "FloatChargeVolt": r(36, 1, regUint, scaled(10), scaleEncode(10)), "BatLowtoUti": r(37, 1, regUint, scaled(10), scaleEncode(10)), "ACChargeAmps": r(38, 1, regUint, asInt, integerEncode),
	"BatteryType": r(39, 1, regUint, enumDecoder(batteryTypeR, false), enumEncoder(batteryTypeW)), "AgingMode": r(40, 1, regUint, enumDecoder(agingR, false), enumEncoder(agingW)), "FunctionMask": r(41, 1, regUint, asInt, integerEncode), "SafetyType": r(42, 1, regUint, enumDecoder(safetyR, false), enumEncoder(safetyW)), "DTC": r(43, 1, regUint, asInt, nil),
	"SysYear": r(45, 1, regUint, asInt, integerEncode), "SysMonth": r(46, 1, regUint, asInt, integerEncode), "SysDay": r(47, 1, regUint, asInt, integerEncode), "SysHour": r(48, 1, regUint, asInt, integerEncode), "SysMin": r(49, 1, regUint, asInt, integerEncode), "SysSec": r(50, 1, regUint, asInt, integerEncode),
	"HoldingChipSelect": r(51, 1, regUint, asInt, nil), "uwAcVHighL": r(52, 1, regUint, asInt, nil), "uwAcVLowL": r(53, 1, regUint, asInt, nil), "uwAcFreqHighL": r(54, 1, regUint, asInt, nil), "uwAcFreqLowL": r(55, 1, regUint, asInt, nil), "HoldingVar1Setting": r(56, 1, regUint, asInt, nil), "DebugModeEnable": r(57, 1, regUint, boolDecode, nil),
	"ManufacturerInfo": r(59, 8, regChar, identity, nil), "ControlFWBuildNo2": r(67, 1, regUint, asInt, nil), "ControlFWBuildNo1": r(68, 1, regUint, asInt, nil), "ComFWBuildNo2": r(69, 1, regUint, asInt, nil), "ComFWBuildNo1": r(70, 1, regUint, asInt, nil),
	"SysWeekly": r(72, 1, regUint, asInt, integerEncode), "ModbusVersion": r(73, 1, regUint, asInt, nil), "SCCComMode": r(75, 1, regUint, asInt, nil), "RateWatt": r(76, 2, regUint, scaled(10), nil), "RateVA": r(78, 2, regUint, scaled(10), nil), "ComboardVer": r(80, 1, regUint, asInt, nil), "uwBatPieceNum": r(81, 1, regUint, asInt, integerEncode), "wBatLowCutOff": r(82, 1, regUint, scaled(10), nil), "MaxGeneratorChargeAmps": r(83, 1, regUint, asInt, nil),
	"NomGridVRaw": r(84, 1, regUint, asInt, nil), "NomGridFreqRaw": r(85, 1, regUint, asInt, nil), "NomBatVRaw": r(86, 1, regUint, asInt, nil), "NomPVCurrRaw": r(87, 1, regUint, asInt, nil), "NomAcChgCurrRaw": r(88, 1, regUint, asInt, nil), "NomOpVRaw": r(89, 1, regUint, asInt, nil), "NomOpFreqRaw": r(90, 1, regUint, asInt, nil), "NomOpPowRaw": r(91, 1, regUint, asInt, nil),
	"uwAC2BatVolt": r(95, 1, regUint, scaled(10), scaleEncode(10)), "BypEnable": r(96, 1, regUint, boolDecode, boolEncode), "PowSavingEnable": r(97, 1, regUint, boolDecode, boolEncode), "SpowBalEnable": r(98, 1, regUint, boolDecode, boolEncode), "ClrEnergyToday": r(99, 1, regUint, boolDecode, boolEncode), "ClrEnergyAll": r(100, 1, regUint, boolDecode, boolEncode), "BurnInTestEnable": r(101, 1, regUint, boolDecode, boolEncode), "ManualStartEnable": r(102, 1, regUint, boolDecode, boolEncode), "SciLossChkEnable": r(103, 1, regUint, boolDecode, boolEncode), "BlightEnable": r(104, 1, regUint, boolDecode, boolEncode), "ParaMaxChgAmps": r(105, 1, regUint, asInt, nil), "LiProtocolType": r(106, 1, regUint, asInt, integerEncode), "AudioAlarmEnable": r(107, 1, regUint, boolDecode, boolEncode),
}

func combineRegisters(registers []uint16, start, length int, signed bool) (int64, error) {
	if start < 0 || length < 1 || start+length > len(registers) {
		return 0, fmt.Errorf("register range out of bounds")
	}
	if length > 4 {
		return 0, fmt.Errorf("numeric register too wide")
	}
	var u uint64
	for _, v := range registers[start : start+length] {
		u = u<<16 | uint64(v)
	}
	if signed {
		bits := length * 16
		if u&(uint64(1)<<uint(bits-1)) != 0 {
			return int64(u - (uint64(1) << uint(bits))), nil
		}
	}
	return int64(u), nil
}
func registersToString(registers []uint16, start, length int) (string, error) {
	if start < 0 || start+length > len(registers) {
		return "", fmt.Errorf("register range out of bounds")
	}
	b := make([]byte, length*2)
	for i, v := range registers[start : start+length] {
		binary.BigEndian.PutUint16(b[i*2:], v)
	}
	b = []byte(strings.TrimRight(string(b), "\x00"))
	if utf8.Valid(b) {
		return string(b), nil
	}
	return strings.ToValidUTF8(string(b), "�"), nil
}
func rawValue(registers []uint16, d registerDef) (any, error) {
	if d.Kind == regChar {
		return registersToString(registers, d.Start, d.Length)
	}
	return combineRegisters(registers, d.Start, d.Length, d.Kind == regInt)
}
func decodeTable(registers []uint16, table map[string]registerDef) map[string]any {
	out := make(map[string]any)
	for key, d := range table {
		raw, e := rawValue(registers, d)
		if e != nil {
			continue
		}
		v, e := d.Decode(raw)
		if e == nil {
			out[key] = v
		}
	}
	return out
}

func encodeRegisters(value any, d registerDef) ([]uint16, error) {
	processed, e := d.Encode(value)
	if e != nil {
		return nil, e
	}
	if d.Kind == regChar {
		s := []byte(processed.(string))
		words := make([]uint16, 0, (len(s)+1)/2)
		for i := 0; i < len(s); i += 2 {
			if i+1 < len(s) {
				words = append(words, uint16(s[i])<<8|uint16(s[i+1]))
			} else {
				words = append(words, uint16(s[i]))
			}
		}
		if len(words) > d.Length {
			return nil, fmt.Errorf("invalid value length")
		}
		for len(words) < d.Length {
			words = append(words, 0)
		}
		return words, nil
	}
	x, ok := processed.(int64)
	if !ok {
		return nil, fmt.Errorf("integer encoder result expected")
	}
	bits := d.Length * 16
	var u uint64
	if d.Kind == regInt {
		min := -int64(1) << uint(bits-1)
		maxv := (int64(1) << uint(bits-1)) - 1
		if x < min || x > maxv {
			return nil, fmt.Errorf("value out of range")
		}
		u = uint64(x) & ((uint64(1) << uint(bits)) - 1)
	} else {
		if x < 0 || (bits < 64 && uint64(x) > ((uint64(1)<<uint(bits))-1)) {
			return nil, fmt.Errorf("value out of range")
		}
		u = uint64(x)
	}
	out := make([]uint16, d.Length)
	for i := d.Length - 1; i >= 0; i-- {
		out[i] = uint16(u)
		u >>= 16
	}
	return out, nil
}

type registerWindow struct{ Start, Count int }

func buildRegisterWindows(table map[string]registerDef, maxWindow int) []registerWindow {
	defs := make([]registerDef, 0, len(table))
	for _, d := range table {
		defs = append(defs, d)
	}
	sortRegisterDefs(defs)
	type span struct{ s, e int }
	spans := []span{}
	for _, d := range defs {
		end := d.Start + d.Length - 1
		if len(spans) > 0 && max(spans[len(spans)-1].e, end)-min(spans[len(spans)-1].s, d.Start)+1 <= 125 {
			if end > spans[len(spans)-1].e {
				spans[len(spans)-1].e = end
			}
		} else {
			spans = append(spans, span{d.Start, end})
		}
	}
	out := []registerWindow{}
	for _, p := range spans {
		for cur := p.s; cur <= p.e; {
			n := min(maxWindow, p.e-cur+1)
			out = append(out, registerWindow{cur, n})
			cur += n
		}
	}
	return out
}
func sortRegisterDefs(a []registerDef) {
	for i := 1; i < len(a); i++ {
		for j := i; j > 0 && a[j].Start < a[j-1].Start; j-- {
			a[j], a[j-1] = a[j-1], a[j]
		}
	}
}

var inputRegisterWindows = buildRegisterWindows(inputRegisters, 45)
var holdingRegisterWindows = buildRegisterWindows(holdingRegisters, 45)
