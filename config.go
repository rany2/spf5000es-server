package main

import (
	"bufio"
	"fmt"
	"math"
	"os"
	"strconv"
	"strings"
	"time"
)

type ModbusConfig struct {
	Port            string
	WriteQueueSize  int
	WriteBatchDelay time.Duration
	Timeout         time.Duration
	Retries         int
	ReconnectDelay  time.Duration
}

type MQTTConfig struct {
	Host                         string
	Port                         int
	Username, Password           string
	ClientID                     string
	Keepalive                    time.Duration
	TopicPrefix, DiscoveryPrefix string
	DeviceID, DeviceName         string
	ConfigInterval               time.Duration
	StatusInterval               time.Duration
}

type AppConfig struct {
	Modbus   ModbusConfig
	MQTT     MQTTConfig
	LogLevel string
}

func parseINI(path string) (map[string]map[string]string, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	data := make(map[string]map[string]string)
	section := ""
	s := bufio.NewScanner(f)
	for line := 1; s.Scan(); line++ {
		v := strings.TrimSpace(s.Text())
		if v == "" || strings.HasPrefix(v, ";") || strings.HasPrefix(v, "#") {
			continue
		}
		if strings.HasPrefix(v, "[") && strings.HasSuffix(v, "]") {
			section = strings.ToUpper(strings.TrimSpace(v[1 : len(v)-1]))
			if data[section] == nil {
				data[section] = make(map[string]string)
			}
			continue
		}
		key, value, ok := strings.Cut(v, "=")
		if !ok {
			return nil, fmt.Errorf("%s:%d: invalid INI line", path, line)
		}
		if data[section] == nil {
			data[section] = make(map[string]string)
		}
		data[section][strings.ToUpper(strings.TrimSpace(key))] = strings.TrimSpace(value)
	}
	return data, s.Err()
}

func ReadAppConfig(path string) (AppConfig, error) {
	ini, err := parseINI(path)
	if err != nil {
		return AppConfig{}, err
	}
	get := func(section, key, fallback string) string {
		if v, ok := ini[section][key]; ok {
			return v
		}
		return fallback
	}
	requiredPort := get("MODBUS", "PORT", "")
	if requiredPort == "" {
		return AppConfig{}, fmt.Errorf("missing MODBUS.PORT")
	}
	intv := func(s, k string, d int) (int, error) {
		v := get(s, k, strconv.Itoa(d))
		n, e := strconv.Atoi(v)
		if e != nil {
			return 0, fmt.Errorf("invalid %s.%s: %w", s, k, e)
		}
		return n, nil
	}
	floatv := func(s, k string, d float64) (float64, error) {
		v := get(s, k, strconv.FormatFloat(d, 'f', -1, 64))
		n, e := strconv.ParseFloat(v, 64)
		if e != nil {
			return 0, fmt.Errorf("invalid %s.%s: %w", s, k, e)
		}
		if math.IsNaN(n) || math.IsInf(n, 0) {
			return 0, fmt.Errorf("invalid %s.%s: finite number required", s, k)
		}
		return n, nil
	}
	wq, e := intv("MODBUS", "WRITE_QUEUE_SIZE", 128)
	if e != nil {
		return AppConfig{}, e
	}
	retries, e := intv("MODBUS", "RETRIES", 2)
	if e != nil {
		return AppConfig{}, e
	}
	mbd, e := floatv("MODBUS", "WRITE_BATCH_DELAY_SEC", .25)
	if e != nil {
		return AppConfig{}, e
	}
	timeout, e := floatv("MODBUS", "TIMEOUT_SEC", 1.5)
	if e != nil {
		return AppConfig{}, e
	}
	rd, e := floatv("MODBUS", "RECONNECT_DELAY_SEC", .2)
	if e != nil {
		return AppConfig{}, e
	}
	deviceID := get("MQTT", "DEVICE_ID", "growatt_spf5000es")
	port, e := intv("MQTT", "PORT", 1883)
	if e != nil {
		return AppConfig{}, e
	}
	keep, e := intv("MQTT", "KEEPALIVE_SEC", 60)
	if e != nil {
		return AppConfig{}, e
	}
	ci, e := floatv("MQTT", "CONFIG_INTERVAL_SEC", 1800)
	if e != nil {
		return AppConfig{}, e
	}
	si, e := floatv("MQTT", "STATUS_INTERVAL_SEC", 1)
	if e != nil {
		return AppConfig{}, e
	}
	if wq < 1 {
		return AppConfig{}, fmt.Errorf("MODBUS.WRITE_QUEUE_SIZE must be positive")
	}
	if retries < 0 {
		return AppConfig{}, fmt.Errorf("MODBUS.RETRIES must be non-negative")
	}
	if mbd < 0 || rd < 0 {
		return AppConfig{}, fmt.Errorf("Modbus delays must be non-negative")
	}
	if timeout < .1 {
		return AppConfig{}, fmt.Errorf("MODBUS.TIMEOUT_SEC must be at least 0.1")
	}
	if port < 1 || port > 65535 {
		return AppConfig{}, fmt.Errorf("MQTT.PORT must be between 1 and 65535")
	}
	if keep < 1 {
		return AppConfig{}, fmt.Errorf("MQTT.KEEPALIVE_SEC must be positive")
	}
	if ci < 1 || si < .1 {
		return AppConfig{}, fmt.Errorf("MQTT intervals are below the supported minimum")
	}
	maxDurationSeconds := float64(math.MaxInt64) / float64(time.Second)
	for name, seconds := range map[string]float64{
		"MODBUS.WRITE_BATCH_DELAY_SEC": mbd,
		"MODBUS.TIMEOUT_SEC":           timeout,
		"MODBUS.RECONNECT_DELAY_SEC":   rd,
		"MQTT.CONFIG_INTERVAL_SEC":     ci,
		"MQTT.STATUS_INTERVAL_SEC":     si,
	} {
		if seconds > maxDurationSeconds {
			return AppConfig{}, fmt.Errorf("%s is too large", name)
		}
	}
	optional := func(v string) string {
		v = strings.TrimSpace(v)
		switch strings.ToLower(v) {
		case "", "none", "null", "false":
			return ""
		}
		return v
	}
	host := strings.TrimSpace(get("MQTT", "HOST", "localhost"))
	deviceID = strings.TrimSpace(deviceID)
	clientID := strings.TrimSpace(get("MQTT", "CLIENT_ID", deviceID))
	if host == "" || deviceID == "" || clientID == "" {
		return AppConfig{}, fmt.Errorf("MQTT.HOST, MQTT.DEVICE_ID, and MQTT.CLIENT_ID must not be empty")
	}
	cfg := AppConfig{
		Modbus: ModbusConfig{
			Port: requiredPort, WriteQueueSize: wq,
			WriteBatchDelay: time.Duration(mbd * float64(time.Second)), Timeout: time.Duration(timeout * float64(time.Second)),
			Retries: retries, ReconnectDelay: time.Duration(rd * float64(time.Second)),
		},
		MQTT: MQTTConfig{
			Host: host, Port: port, Username: optional(get("MQTT", "USER", "")), Password: optional(get("MQTT", "PASSWORD", "")),
			ClientID: clientID, Keepalive: time.Duration(keep) * time.Second,
			TopicPrefix: strings.Trim(get("MQTT", "TOPIC_PREFIX", deviceID), "/"), DiscoveryPrefix: strings.Trim(get("MQTT", "DISCOVERY_PREFIX", "homeassistant"), "/"),
			DeviceID: deviceID, DeviceName: get("MQTT", "DEVICE_NAME", "Growatt SPF 5000 ES"),
			ConfigInterval: time.Duration(ci * float64(time.Second)), StatusInterval: time.Duration(si * float64(time.Second)),
		},
		LogLevel: get("LOGGING", "LEVEL", "INFO"),
	}
	if cfg.MQTT.TopicPrefix == "" {
		cfg.MQTT.TopicPrefix = deviceID
	}
	if cfg.MQTT.DiscoveryPrefix == "" {
		cfg.MQTT.DiscoveryPrefix = "homeassistant"
	}
	return cfg, nil
}
