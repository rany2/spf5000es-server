package main

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"math"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode"

	mqtt "github.com/eclipse/paho.mqtt.golang"
)

const (
	defaultStatusInterval        = 250 * time.Millisecond
	statusFullSnapshotInterval   = time.Minute
	statusFailureBackoffMax      = 30 * time.Second
	configFailureRetry           = 30 * time.Second
	configVerifyRetry            = 2 * time.Second
	commandQueueMax              = 32
	inverterOfflineAfterFailures = 3
	mqttOperationTimeout         = 5 * time.Second
)

type mqttPublisher interface {
	Publish(string, any, bool) error
	Subscribe(string) error
	Disconnect()
}
type pahoPublisher struct{ client mqtt.Client }

func (p *pahoPublisher) Publish(topic string, value any, retained bool) error {
	token := p.client.Publish(topic, 0, retained, value)
	if !token.WaitTimeout(mqttOperationTimeout) {
		return fmt.Errorf("MQTT publish timed out")
	}
	return token.Error()
}
func (p *pahoPublisher) Subscribe(topic string) error {
	token := p.client.Subscribe(topic, 0, nil)
	if !token.WaitTimeout(mqttOperationTimeout) {
		return fmt.Errorf("MQTT subscribe timed out")
	}
	return token.Error()
}
func (p *pahoPublisher) Disconnect() { p.client.Disconnect(1000) }

type mqttCommand struct{ topic, payload string }
type MQTTService struct {
	mu                sync.Mutex
	inverter          inverterAPI
	config            MQTTConfig
	scheduler         *Scheduler
	client            mqttPublisher
	connected         bool
	inverterAvailable *bool
	batteryType       string
	statusRetry       time.Duration
	lastStatus        map[string]string
	lastFullStatus    time.Time
	statusGeneration  uint64
	commands          []mqttCommand
	slugToKey         map[string]string
}

func NewMQTTService(inv inverterAPI, cfg MQTTConfig, s *Scheduler) *MQTTService {
	if cfg.StatusInterval <= 0 {
		cfg.StatusInterval = defaultStatusInterval
	}
	m := &MQTTService{inverter: inv, config: cfg, scheduler: s, statusRetry: cfg.StatusInterval, lastStatus: make(map[string]string), slugToKey: make(map[string]string)}
	for key := range holdingRegisters {
		m.slugToKey[slug(key)] = key
	}
	opts := mqtt.NewClientOptions().AddBroker(fmt.Sprintf("tcp://%s:%d", cfg.Host, cfg.Port)).SetClientID(cfg.ClientID).SetKeepAlive(cfg.Keepalive).SetAutoReconnect(true).SetConnectRetry(true).SetConnectRetryInterval(time.Second).SetMaxReconnectInterval(30*time.Second).SetWill(m.availabilityTopic(), "offline", 0, true)
	if cfg.Username != "" {
		opts.SetUsername(cfg.Username)
		opts.SetPassword(cfg.Password)
	}
	opts.SetOnConnectHandler(func(_ mqtt.Client) { m.onConnect() })
	opts.SetConnectionLostHandler(func(_ mqtt.Client, e error) {
		m.mu.Lock()
		m.connected = false
		m.mu.Unlock()
		slog.Warn("MQTT disconnected", "error", e)
	})
	opts.SetDefaultPublishHandler(func(_ mqtt.Client, msg mqtt.Message) { m.onMessage(msg.Topic(), string(msg.Payload())) })
	m.client = &pahoPublisher{client: mqtt.NewClient(opts)}
	mustRegister(s, taskMQTTCommands, m.drainCommands, 0, 20)
	mustRegister(s, taskMQTTConfig, m.PublishConfig, cfg.ConfigInterval, 40)
	mustRegister(s, taskMQTTStatus, m.PublishStatus, cfg.StatusInterval, 50)
	return m
}
func mustRegister(s *Scheduler, n string, f func(), d time.Duration, p int) {
	must(s.Register(n, f, d, p))
}
func (m *MQTTService) baseTopic() string         { return strings.Trim(m.config.TopicPrefix, "/") }
func (m *MQTTService) availabilityTopic() string { return m.baseTopic() + "/availability" }
func (m *MQTTService) inverterAvailabilityTopic() string {
	return m.baseTopic() + "/inverter/availability"
}

func (m *MQTTService) Start() error {
	p := m.client.(*pahoPublisher)
	token := p.client.Connect() // asynchronous; ConnectRetry keeps trying if the broker is down
	go func() {
		token.Wait()
		if err := token.Error(); err != nil {
			slog.Error("MQTT connection failed", "error", err)
		}
	}()
	return nil
}
func (m *MQTTService) Stop() {
	if err := m.client.Publish(m.availabilityTopic(), "offline", true); err != nil {
		slog.Warn("failed to publish offline availability", "error", err)
	}
	m.client.Disconnect()
}
func (m *MQTTService) onConnect() {
	m.mu.Lock()
	m.connected = true
	m.inverterAvailable = nil
	m.lastStatus = make(map[string]string)
	m.lastFullStatus = time.Time{}
	m.statusGeneration++
	m.mu.Unlock()
	m.publish(m.availabilityTopic(), "online", true)
	for _, topic := range []string{m.baseTopic() + "/config/+/set", m.baseTopic() + "/time_sync/set"} {
		if err := m.client.Subscribe(topic); err != nil {
			slog.Error("MQTT subscribe failed", "topic", topic, "error", err)
		}
	}
	m.publishDiscovery()
	must(m.scheduler.Schedule(taskMQTTConfig, 0, true))
	must(m.scheduler.Schedule(taskMQTTStatus, 0, true))
	slog.Info("MQTT connected")
}
func (m *MQTTService) onMessage(topic, payload string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if len(m.commands) >= commandQueueMax {
		slog.Warn("dropping MQTT command because queue is full", "topic", topic)
		return
	}
	m.commands = append(m.commands, mqttCommand{topic, strings.TrimSpace(payload)})
	must(m.scheduler.Schedule(taskMQTTCommands, 0, true))
}
func (m *MQTTService) drainCommands() {
	for {
		m.mu.Lock()
		if len(m.commands) == 0 {
			m.mu.Unlock()
			return
		}
		c := m.commands[0]
		m.commands = m.commands[1:]
		m.mu.Unlock()
		m.handleCommand(c.topic, c.payload)
	}
}
func (m *MQTTService) handleCommand(topic, payload string) {
	if topic == m.baseTopic()+"/time_sync/set" {
		m.inverter.SyncTime()
		must(m.scheduler.Schedule(taskMQTTConfig, 0, true))
		return
	}
	prefix := m.baseTopic() + "/config/"
	if !strings.HasPrefix(topic, prefix) || !strings.HasSuffix(topic, "/set") {
		return
	}
	key, ok := m.slugToKey[strings.TrimSuffix(strings.TrimPrefix(topic, prefix), "/set")]
	if !ok {
		return
	}
	expected, e := m.inverter.WriteConfig(key, parseCommandPayload(payload))
	if e != nil {
		slog.Warn("rejected MQTT config command", "key", key, "error", e)
		return
	}
	if expected != nil {
		m.publish(valueTopic(m.baseTopic(), "config", key), mqttValue(expected), true)
	}
	delay := max(time.Second, m.inverter.WriteBatchDelay()+configWriteSettleDelay)
	must(m.scheduler.Schedule(taskMQTTConfig, delay, true))
	slog.Info("accepted MQTT config command", "key", key)
}
func parseCommandPayload(s string) any {
	switch strings.ToLower(s) {
	case "true", "on":
		return true
	case "false", "off":
		return false
	}
	var v any
	if json.Unmarshal([]byte(s), &v) == nil {
		switch v.(type) {
		case string, float64, bool:
			return v
		}
	}
	if f, e := strconv.ParseFloat(s, 64); e == nil && !math.IsNaN(f) && !math.IsInf(f, 0) {
		if math.Abs(math.Round(f)-f) < 1e-3 {
			return int(math.Round(f))
		}
		return f
	}
	return s
}
func mqttValue(v any) string {
	if b, ok := v.(bool); ok {
		if b {
			return "true"
		}
		return "false"
	}
	return fmt.Sprint(v)
}

func (m *MQTTService) publish(topic string, value any, retained bool) bool {
	if err := m.client.Publish(topic, value, retained); err != nil {
		slog.Warn("MQTT publish failed", "topic", topic, "error", err)
		return false
	}
	return true
}

func wordBoundary(prev string, ch, next rune, has bool) bool {
	if !has || !unicode.IsUpper(ch) {
		return false
	}
	p := []rune(prev)
	if len(p) == 0 {
		return false
	}
	last := p[len(p)-1]
	return unicode.IsLower(last) || unicode.IsDigit(last) || (unicode.IsUpper(last) && unicode.IsLower(next))
}
func slug(v string) string {
	r := []rune(v)
	var b strings.Builder
	sep := false
	for n, ch := range r {
		prev := ""
		if n > 0 {
			prev = string(r[n-1])
		}
		var next rune
		if n+1 < len(r) {
			next = r[n+1]
		}
		if wordBoundary(prev, ch, next, n > 0) && !sep {
			b.WriteByte('_')
		}
		if unicode.IsLetter(ch) || unicode.IsDigit(ch) {
			b.WriteRune(unicode.ToLower(ch))
			sep = false
		} else if !sep {
			b.WriteByte('_')
			sep = true
		}
	}
	return strings.Trim(b.String(), "_")
}
func friendlyName(v string) string {
	r := []rune(v)
	words := []string{}
	cur := ""
	for n, ch := range r {
		prev := ""
		if n > 0 {
			prev = string(r[n-1])
		}
		var next rune
		if n+1 < len(r) {
			next = r[n+1]
		}
		if wordBoundary(prev, ch, next, cur != "") {
			words = append(words, cur)
			cur = string(ch)
		} else {
			cur += string(ch)
		}
	}
	if cur != "" {
		words = append(words, cur)
	}
	return strings.Join(words, " ")
}
func valueTopic(base, namespace, key string) string {
	return fmt.Sprintf("%s/%s/%s/state", base, namespace, slug(key))
}

var selectOptions = map[string][]string{"OutputConfig": {"SBU", "SOL", "UTI", "SUB"}, "ChargeConfig": {"PV First", "PV&UTI", "PV Only"}, "PVModel": {"Independent", "Parallel"}, "ACInModel": {"APL", "UPS", "GEN"}, "OutputVoltType": {"208VAC", "230VAC", "240VAC", "220VAC", "100VAC", "110VAC", "120VAC"}, "OutputFreqType": {"50Hz", "60Hz"}, "OverLoadRestart": {"Yes", "No", "Switch to UTI"}, "BatteryType": {"AGM", "FLD", "USE", "Lithium", "USE2"}, "AgingMode": {"Normal", "Aging"}, "SafetyType": {"Standard", "ETL", "AS4777", "CQC", "VDE4105"}}
var booleanKeys = map[string]bool{"OverTempRestart": true, "BuzzerEnable": true, "BypEnable": true, "PowSavingEnable": true, "SpowBalEnable": true, "ClrEnergyToday": true, "ClrEnergyAll": true, "BurnInTestEnable": true, "ManualStartEnable": true, "SciLossChkEnable": true, "BlightEnable": true, "AudioAlarmEnable": true}
var entityMetadata = map[string]map[string]any{
	"SystemStatus": {"icon": "mdi:solar-power"}, "FaultBit": {"icon": "mdi:alert-circle-outline"}, "WarningBit": {"icon": "mdi:alert-outline"}, "WarningBitHigh": {"icon": "mdi:alert-outline"}, "WarningValue": {"icon": "mdi:alert-outline"}, "DeviceTypeCode": {"icon": "mdi:identifier"}, "WorkTimeTotalSeconds": {"icon": "mdi:timer-outline"},
	"OutputConfig": {"icon": "mdi:transmission-tower-export"}, "ChargeConfig": {"icon": "mdi:battery-charging"}, "UtiOutStart": {"icon": "mdi:clock-start", "unit_of_measurement": "h"}, "UtiOutEnd": {"icon": "mdi:clock-end", "unit_of_measurement": "h"}, "UtiChargeStart": {"icon": "mdi:battery-clock", "unit_of_measurement": "h"}, "UtiChargeEnd": {"icon": "mdi:battery-clock", "unit_of_measurement": "h"}, "PVModel": {"icon": "mdi:solar-panel"}, "ACInModel": {"icon": "mdi:transmission-tower-import"},
	"FWVersion": {"icon": "mdi:chip"}, "FWVersion2": {"icon": "mdi:chip"}, "LCDLanguage": {"icon": "mdi:translate"}, "SerialNumber": {"icon": "mdi:barcode"}, "MoudleH": {"icon": "mdi:chip"}, "MoudleL": {"icon": "mdi:chip"}, "ComAddress": {"icon": "mdi:serial-port"}, "FlashStart": {"icon": "mdi:flash"}, "ResetUserInfo": {"icon": "mdi:account-sync-outline"}, "ResetToFactory": {"icon": "mdi:factory"}, "BatteryType": {"icon": "mdi:car-battery"}, "AgingMode": {"icon": "mdi:timer-sand"}, "FunctionMask": {"icon": "mdi:bitwise"}, "SafetyType": {"icon": "mdi:shield-check-outline"}, "DTC": {"icon": "mdi:alert-decagram-outline"}, "SysYear": {"icon": "mdi:calendar"}, "SysMonth": {"icon": "mdi:calendar-month"}, "SysDay": {"icon": "mdi:calendar-today"}, "SysHour": {"icon": "mdi:clock-outline", "unit_of_measurement": "h"}, "SysMin": {"icon": "mdi:clock-outline", "unit_of_measurement": "min"}, "SysSec": {"icon": "mdi:clock-outline", "unit_of_measurement": "s"}, "ManufacturerInfo": {"icon": "mdi:factory"}, "ControlFWBuildNo2": {"icon": "mdi:chip"}, "ControlFWBuildNo1": {"icon": "mdi:chip"}, "ComFWBuildNo2": {"icon": "mdi:chip"}, "ComFWBuildNo1": {"icon": "mdi:chip"}, "SysWeekly": {"icon": "mdi:calendar-week"}, "ModbusVersion": {"icon": "mdi:protocol"}, "SCCComMode": {"icon": "mdi:connection"}, "ComboardVer": {"icon": "mdi:chip"}, "uwBatPieceNum": {"icon": "mdi:battery-multiple"}, "uwAC2BatVolt": {"name": "uw AC2 Bat"}, "LiProtocolType": {"icon": "mdi:protocol"}, "BLVersion2": {"icon": "mdi:chip"},
}

func sensorMetadata(key string) map[string]any {
	out := map[string]any{}
	for k, v := range entityMetadata[key] {
		out[k] = v
	}
	lower := strings.ToLower(key)
	switch {
	case strings.Contains(lower, "seconds") || (strings.Contains(lower, "time") && (strings.Contains(lower, "volt") || strings.Contains(lower, "freq"))):
		out["device_class"] = "duration"
		out["icon"] = "mdi:timer-outline"
		out["unit_of_measurement"] = "s"
	case strings.HasSuffix(lower, "kwh"):
		out["device_class"] = "energy"
		out["icon"] = "mdi:lightning-bolt"
		out["unit_of_measurement"] = "kWh"
		out["state_class"] = "total_increasing"
	case strings.HasSuffix(lower, "kw"):
		out["device_class"] = "power"
		out["icon"] = "mdi:flash"
		out["unit_of_measurement"] = "kW"
	case strings.Contains(lower, "percent") || strings.HasSuffix(lower, "soc"):
		out["icon"] = "mdi:percent-outline"
		out["unit_of_measurement"] = "%"
	case strings.Contains(lower, "temp") && strings.HasSuffix(lower, "c"):
		out["device_class"] = "temperature"
		out["icon"] = "mdi:thermometer"
		out["unit_of_measurement"] = "°C"
	case strings.Contains(lower, "watt"):
		out["device_class"] = "power"
		out["icon"] = "mdi:flash"
		out["unit_of_measurement"] = "W"
	case strings.Contains(lower, "volt"):
		out["device_class"] = "voltage"
		out["icon"] = "mdi:sine-wave"
		out["unit_of_measurement"] = "V"
	case strings.HasSuffix(lower, "va"):
		out["device_class"] = "apparent_power"
		out["icon"] = "mdi:flash-triangle-outline"
		out["unit_of_measurement"] = "VA"
	case strings.Contains(lower, "amps"):
		out["device_class"] = "current"
		out["icon"] = "mdi:current-ac"
		out["unit_of_measurement"] = "A"
	case strings.Contains(lower, "freq"):
		out["device_class"] = "frequency"
		out["icon"] = "mdi:sine-wave"
		out["unit_of_measurement"] = "Hz"
	}
	if _, ok := out["icon"]; !ok {
		switch {
		case strings.Contains(lower, "fan"):
			out["icon"] = "mdi:fan"
		case strings.Contains(lower, "battery") || strings.HasPrefix(lower, "bat"):
			out["icon"] = "mdi:battery"
		case strings.Contains(lower, "pv"):
			out["icon"] = "mdi:solar-panel"
		case strings.Contains(lower, "grid") || strings.Contains(lower, "uti"):
			out["icon"] = "mdi:transmission-tower"
		case strings.Contains(lower, "output"):
			out["icon"] = "mdi:power-plug-outline"
		case strings.Contains(lower, "buzzer") || strings.Contains(lower, "alarm"):
			out["icon"] = "mdi:bell-ring-outline"
		case strings.Contains(lower, "restart") || strings.Contains(lower, "reset"):
			out["icon"] = "mdi:restart"
		case strings.Contains(lower, "enable"):
			out["icon"] = "mdi:toggle-switch-outline"
		}
	}
	if _, ok := out["unit_of_measurement"]; ok && out["device_class"] != "duration" {
		if _, ok := out["state_class"]; !ok {
			out["state_class"] = "measurement"
		}
	}
	return out
}

func (m *MQTTService) devicePayload() map[string]any {
	return map[string]any{"identifiers": []string{m.config.DeviceID}, "name": m.config.DeviceName, "manufacturer": "Growatt", "model": "SPF 5000 ES"}
}
func (m *MQTTService) entityBase(objectID, name string) map[string]any {
	return map[string]any{"name": name, "object_id": objectID, "unique_id": m.config.DeviceID + "_" + objectID, "availability": []map[string]string{{"topic": m.availabilityTopic()}, {"topic": m.inverterAvailabilityTopic()}}, "availability_mode": "all", "device": m.devicePayload()}
}
func merge(dst, src map[string]any) {
	for k, v := range src {
		dst[k] = v
	}
}
func (m *MQTTService) publishDiscoveryPayload(component, objectID string, p map[string]any) {
	b, e := json.Marshal(p)
	if e != nil {
		slog.Error("failed to encode discovery payload", "error", e)
		return
	}
	m.publish(fmt.Sprintf("%s/%s/%s/%s/config", m.config.DiscoveryPrefix, component, m.config.DeviceID, objectID), string(b), true)
}
func (m *MQTTService) clearDiscoveryPayload(component, objectID string) {
	m.publish(fmt.Sprintf("%s/%s/%s/%s/config", m.config.DiscoveryPrefix, component, m.config.DeviceID, objectID), "", true)
}

func (m *MQTTService) publishDiscovery() {
	for key := range inputRegisters {
		objectID := m.config.DeviceID + "_" + slug(key)
		p := m.entityBase(objectID, friendlyName(key))
		p["state_topic"] = valueTopic(m.baseTopic(), "status", key)
		merge(p, sensorMetadata(key))
		m.publishDiscoveryPayload("sensor", objectID, p)
	}
	for key, d := range holdingRegisters {
		component, p := m.configEntityPayload(key, d)
		objectID := p["object_id"].(string)
		m.publishDiscoveryPayload(component, objectID, p)
		if component != "sensor" {
			m.clearDiscoveryPayload("sensor", objectID)
		}
	}
	objectID := m.config.DeviceID + "_sync_time"
	p := m.entityBase(objectID, "Sync Time")
	p["command_topic"] = m.baseTopic() + "/time_sync/set"
	p["payload_press"] = "sync"
	p["icon"] = "mdi:clock-sync-outline"
	m.publishDiscoveryPayload("button", objectID, p)
}
func (m *MQTTService) configEntityPayload(key string, d registerDef) (string, map[string]any) {
	objectID := m.config.DeviceID + "_" + slug(key)
	name := friendlyName(key)
	if x := entityMetadata[key]["name"]; x != nil {
		name = x.(string)
	}
	p := m.entityBase(objectID, name)
	p["state_topic"] = valueTopic(m.baseTopic(), "config", key)
	writable := d.Encode != nil
	if writable {
		p["command_topic"] = m.baseTopic() + "/config/" + slug(key) + "/set"
	}
	if d.Decode != nil && (key == "DebugModeEnable" || booleanKeys[key]) {
		p["payload_on"] = "true"
		p["payload_off"] = "false"
		p["icon"] = "mdi:toggle-switch-outline"
		if !writable {
			return "binary_sensor", p
		}
		p["state_on"] = "true"
		p["state_off"] = "false"
		return "switch", p
	}
	if !writable {
		merge(p, sensorMetadata(key))
		return "sensor", p
	}
	if opts, ok := selectOptions[key]; ok {
		merge(p, sensorMetadata(key))
		p["options"] = opts
		return "select", p
	}
	if d.Kind == regChar {
		merge(p, sensorMetadata(key))
		return "text", p
	}
	merge(p, sensorMetadata(key))
	limits := m.numberLimits(key)
	p["min"] = limits.Min
	p["max"] = limits.Max
	p["step"] = limits.Step
	p["mode"] = "box"
	if key == "BatLowtoUti" || key == "uwAC2BatVolt" {
		delete(p, "device_class")
		delete(p, "unit_of_measurement")
		delete(p, "state_class")
		merge(p, m.batteryUnitMetadata())
	}
	return "number", p
}
func (m *MQTTService) numberLimits(key string) numberLimits {
	m.mu.Lock()
	batteryType := m.batteryType
	m.mu.Unlock()
	return configLimits(key, batteryType)
}
func (m *MQTTService) batteryUnitMetadata() map[string]any {
	m.mu.Lock()
	batteryType := m.batteryType
	m.mu.Unlock()
	if batteryType == "" {
		return nil
	}
	if batteryType == "Lithium" {
		return map[string]any{"icon": "mdi:percent-outline", "unit_of_measurement": "%", "state_class": "measurement"}
	}
	return map[string]any{"device_class": "voltage", "icon": "mdi:sine-wave", "unit_of_measurement": "V", "state_class": "measurement"}
}
func (m *MQTTService) refreshBatteryDiscovery(battery string) {
	m.mu.Lock()
	if battery == "" || battery == m.batteryType {
		m.mu.Unlock()
		return
	}
	m.batteryType = battery
	m.mu.Unlock()
	for _, key := range []string{"BatLowtoUti", "uwAC2BatVolt"} {
		c, p := m.configEntityPayload(key, holdingRegisters[key])
		m.publishDiscoveryPayload(c, p["object_id"].(string), p)
	}
}

func (m *MQTTService) isConnected() bool { m.mu.Lock(); defer m.mu.Unlock(); return m.connected }
func (m *MQTTService) publishInverterAvailability() {
	available := m.inverter.ConsecutiveReadFailures() < inverterOfflineAfterFailures
	m.mu.Lock()
	if m.inverterAvailable != nil && *m.inverterAvailable == available {
		m.mu.Unlock()
		return
	}
	m.inverterAvailable = new(bool)
	*m.inverterAvailable = available
	m.mu.Unlock()
	payload := "offline"
	if available {
		payload = "online"
	}
	m.publish(m.inverterAvailabilityTopic(), payload, true)
}
func (m *MQTTService) PublishStatus() {
	if !m.isConnected() {
		return
	}
	status, e := m.inverter.ReadStatus()
	if e != nil {
		m.publishInverterAvailability()
		m.statusRetry = min(statusFailureBackoffMax, m.statusRetry*2)
		must(m.scheduler.Schedule(taskMQTTStatus, m.statusRetry, true))
		return
	}
	m.statusRetry = m.config.StatusInterval
	m.publishInverterAvailability()
	now := m.scheduler.now()
	m.mu.Lock()
	fullSnapshot := m.lastFullStatus.IsZero() || now.Sub(m.lastFullStatus) >= statusFullSnapshotInterval
	generation := m.statusGeneration
	previous := make(map[string]string, len(m.lastStatus))
	for key, value := range m.lastStatus {
		previous[key] = value
	}
	m.mu.Unlock()
	allPublished := true
	published := make(map[string]string)
	for key, value := range status {
		payload := mqttValue(value)
		if !fullSnapshot && previous[key] == payload {
			continue
		}
		if m.publish(valueTopic(m.baseTopic(), "status", key), payload, false) {
			published[key] = payload
		} else {
			allPublished = false
		}
	}
	m.mu.Lock()
	if generation != m.statusGeneration {
		m.mu.Unlock()
		return
	}
	for key, value := range published {
		m.lastStatus[key] = value
	}
	if fullSnapshot && allPublished {
		m.lastFullStatus = now
	}
	m.mu.Unlock()
}
func (m *MQTTService) PublishConfig() {
	if !m.isConnected() {
		return
	}
	cfg, e := m.inverter.ReadConfig()
	if e != nil {
		m.publishInverterAvailability()
		must(m.scheduler.Schedule(taskMQTTConfig, configFailureRetry, true))
		return
	}
	m.publishInverterAvailability()
	if b, ok := cfg["BatteryType"].(string); ok {
		m.refreshBatteryDiscovery(b)
	}
	for key, value := range cfg {
		m.publish(valueTopic(m.baseTopic(), "config", key), mqttValue(value), true)
	}
	if m.inverter.HasPendingReadback() {
		must(m.scheduler.Schedule(taskMQTTConfig, configVerifyRetry, false))
	}
}
