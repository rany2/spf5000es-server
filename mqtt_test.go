package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"testing"
	"time"
)

type publishedMessage struct {
	topic   string
	payload any
	retain  bool
}
type fakeMQTT struct {
	published     []publishedMessage
	subscriptions []string
	publishErr    error
	subscribeErr  error
}

func (f *fakeMQTT) Publish(t string, p any, r bool) error {
	f.published = append(f.published, publishedMessage{t, p, r})
	return f.publishErr
}
func (f *fakeMQTT) Subscribe(t string) error {
	f.subscriptions = append(f.subscriptions, t)
	return f.subscribeErr
}
func (f *fakeMQTT) Disconnect() {}
func mqttTestService() (*MQTTService, *fakeMQTT) {
	i, s, _, _ := testInverter(0)
	cfg := MQTTConfig{Host: "localhost", Port: 1883, ClientID: "test", Keepalive: time.Minute, TopicPrefix: "growatt/spf5000es", DiscoveryPrefix: "homeassistant", DeviceID: "growatt_spf5000es", DeviceName: "Growatt SPF 5000 ES", ConfigInterval: 5 * time.Minute}
	m := NewMQTTService(i, cfg, s)
	f := &fakeMQTT{}
	m.client = f
	return m, f
}
func findPublished(t *testing.T, f *fakeMQTT, topic string) publishedMessage {
	t.Helper()
	for _, m := range f.published {
		if m.topic == topic {
			return m
		}
	}
	t.Fatalf("topic not published: %s", topic)
	return publishedMessage{}
}

func TestSlugAndFriendlyName(t *testing.T) {
	if slug("ACInputWatt") != "ac_input_watt" {
		t.Fatalf("slug=%s", slug("ACInputWatt"))
	}
	if friendlyName("BatterySOC") != "Battery SOC" {
		t.Fatalf("name=%s", friendlyName("BatterySOC"))
	}
}
func TestDiscoverySelectAndNumber(t *testing.T) {
	m, f := mqttTestService()
	m.onConnect()
	selectTopic := "homeassistant/select/growatt_spf5000es/growatt_spf5000es_output_config/config"
	msg := findPublished(t, f, selectTopic)
	var p map[string]any
	if e := json.Unmarshal([]byte(msg.payload.(string)), &p); e != nil {
		t.Fatal(e)
	}
	if p["command_topic"] != "growatt/spf5000es/config/output_config/set" || p["icon"] != "mdi:transmission-tower-export" {
		t.Fatalf("payload=%v", p)
	}
	numTopic := "homeassistant/number/growatt_spf5000es/growatt_spf5000es_sys_year/config"
	msg = findPublished(t, f, numTopic)
	if e := json.Unmarshal([]byte(msg.payload.(string)), &p); e != nil {
		t.Fatal(e)
	}
	if p["min"] != float64(2000) || p["max"] != float64(2099) {
		t.Fatalf("limits=%v", p)
	}
}
func TestBatteryDependentDiscovery(t *testing.T) {
	m, _ := mqttTestService()
	a := m.numberLimits("BatLowtoUti")
	if a.Min != 5 || a.Max != 100 || a.Step != .1 {
		t.Fatalf("fallback=%+v", a)
	}
	m.mu.Lock()
	m.batteryType = "Lithium"
	m.mu.Unlock()
	l := m.numberLimits("BatLowtoUti")
	if l.Min != 5 || l.Max != 100 || l.Step != 1 {
		t.Fatalf("lithium=%+v", l)
	}
	m.mu.Lock()
	m.batteryType = "AGM"
	m.mu.Unlock()
	v := m.numberLimits("BatLowtoUti")
	if v.Min != 20 || v.Max != 64 || v.Step != .1 {
		t.Fatalf("voltage=%+v", v)
	}
}

func TestMetadataSpecificSuffixes(t *testing.T) {
	cases := map[string]struct{ unit, class string }{
		"GridHighVoltLoadReductionWatt1": {"W", "power"},
		"VoltLowLossPercent1":            {"%", ""},
		"VoltLowLossTime1":               {"s", "duration"},
		"FreqReconnectTime":              {"s", "duration"},
		"PVLowLimitWattkW":               {"kW", "power"},
	}
	for key, want := range cases {
		metadata := sensorMetadata(key)
		class := ""
		if metadata["device_class"] != nil {
			class = fmt.Sprint(metadata["device_class"])
		}
		if metadata["unit_of_measurement"] != want.unit || class != want.class {
			t.Errorf("%s metadata = %v", key, metadata)
		}
	}
}
func TestCommandOptimisticEcho(t *testing.T) {
	m, f := mqttTestService()
	m.handleCommand("growatt/spf5000es/config/max_charge_amps/set", "30")
	msg := findPublished(t, f, "growatt/spf5000es/config/max_charge_amps/state")
	if msg.payload != "30" || !msg.retain {
		t.Fatalf("message=%+v", msg)
	}
}

func TestPublishFailureIsReported(t *testing.T) {
	m, f := mqttTestService()
	f.publishErr = errors.New("broker unavailable")
	if m.publish("test/topic", "value", false) {
		t.Fatal("failed publish reported success")
	}
}

func TestStatusPublishesDeltasAndPeriodicSnapshot(t *testing.T) {
	m, f := mqttTestService()
	m.mu.Lock()
	m.connected = true
	m.mu.Unlock()
	m.PublishStatus()
	firstCount := len(f.published)
	if firstCount == 0 {
		t.Fatal("initial snapshot was not published")
	}
	m.PublishStatus()
	if len(f.published) != firstCount {
		t.Fatalf("unchanged status added %d messages", len(f.published)-firstCount)
	}
	m.mu.Lock()
	m.lastFullStatus = m.lastFullStatus.Add(-statusFullSnapshotInterval)
	m.mu.Unlock()
	m.PublishStatus()
	if len(f.published) <= firstCount {
		t.Fatal("periodic full snapshot was not published")
	}
}
