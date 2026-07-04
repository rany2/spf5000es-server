#!/usr/bin/env python3

"""Tests for Modbus validation and recovery behavior."""

# pylint: disable=too-many-lines

import json
from datetime import datetime
import tempfile
import textwrap
import unittest
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

from pymodbus.exceptions import ModbusException

from growatt import (
    HOLDING_REGISTER_WINDOWS,
    INPUT_REGISTER_WINDOWS,
    INPUT_REGISTERS,
    ON_OFF_R,
    RegType,
    TASK_MQTT_CONFIG,
    TASK_MQTT_STATUS,
    TASK_TIME_SYNC,
    GrowattInverter,
    GrowattMqttConfig,
    GrowattMqttService,
    GrowattModbusClient,
    ModbusAppConfig,
    Scheduler,
    WriteQueueFullError,
    read_app_config,
)


def make_modbus_config(**overrides):
    """Build a complete Modbus config for inverter tests."""

    config = {
        "port": "/dev/null",
        "write_queue_size": 128,
        "write_batch_delay_sec": 0.25,
        "timeout_sec": 1.5,
        "retries": 2,
        "reconnect_delay_sec": 0.2,
    }
    config.update(overrides)
    return ModbusAppConfig(**config)


class FakeClock:
    """Deterministic monotonic clock for scheduler tests."""

    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, sec):
        """Move the clock forward."""

        self.now += sec


class ReadResponse:  # pylint: disable=too-few-public-methods
    """Minimal pymodbus-like read response."""

    def __init__(self, registers):
        self.registers = registers

    def isError(self):  # pylint: disable=invalid-name
        """Return whether this response is a Modbus exception response."""

        return False


class WriteResponse:  # pylint: disable=too-few-public-methods
    """Minimal pymodbus-like write response."""

    def __init__(self, address, count):
        self.address = address
        self.count = count

    def isError(self):  # pylint: disable=invalid-name
        """Return whether this response is a Modbus exception response."""

        return False


class FakeSerialClient:
    """Small fake for exercising recovery without serial hardware."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.closed = 0
        self.connected = 0
        self.writes = []

    def connect(self):
        """Pretend the serial port can be reopened."""

        self.connected += 1
        return True

    def close(self):
        """Record that the serial port was closed."""

        self.closed += 1

    def read_input_registers(self, _start, _count):
        """Return queued responses or no response."""

        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return None

    def read_holding_registers(self, start, count):
        """Return queued holding-register responses."""

        return self.read_input_registers(start, count)

    def write_registers(self, start, values):
        """Record a write and return a matching acknowledgement."""

        self.writes.append((start, values))
        return WriteResponse(start, len(values))


class FakeGrowattClient:
    """Small fake for exercising inverter scheduling without serial hardware."""

    def __init__(self):
        self.writes = []
        self.deferred = []
        self.ready_waits = 0
        self.fail_reads = False
        self.holding = {}

    def write_registers(self, start, values):
        """Record a Modbus write."""

        self.writes.append((start, values))

    def apply_writes(self):
        """Make recorded writes visible to holding-register reads."""

        for start, values in self.writes:
            for offset, value in enumerate(values):
                self.holding[start + offset] = value

    def read_holding_registers(self, start, count):
        """Return holding registers from the fake device state."""

        if self.fail_reads:
            raise ModbusException("simulated read failure")
        return [self.holding.get(start + i, 0) for i in range(count)]

    def defer_next_operations(self, delay_sec):
        """Record deferred Modbus timing."""

        self.deferred.append(delay_sec)

    def wait_until_ready_for_operation(self):
        """Record readiness waits."""

        self.ready_waits += 1

    def read_input_registers(self, _start, count):
        """Return zeroed registers or simulate a failed window read."""

        if self.fail_reads:
            raise ModbusException("simulated read failure")
        return [0] * count


class FakeMqttClient:  # pylint: disable=too-many-instance-attributes
    """Small fake for exercising MQTT discovery without a broker."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.published = []
        self.subscriptions = []
        self.username = None
        self.password = None
        self.will = None
        self.connect_args = None
        self.max_queued_messages = None
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None
        self.reconnect_delay = None

    def max_queued_messages_set(self, queue_size):
        """Record outgoing queue sizing."""

        self.max_queued_messages = queue_size

    def username_pw_set(self, username, password=None):
        """Record configured credentials."""

        self.username = username
        self.password = password

    def will_set(self, topic, payload=None, retain=False):
        """Record the configured LWT."""

        self.will = (topic, payload, retain)

    def connect(self, host, port, keepalive):
        """Record a connect request."""

        self.connect_args = (host, port, keepalive)

    def reconnect_delay_set(self, min_delay=1, max_delay=120):
        """Record reconnect backoff configuration."""

        self.reconnect_delay = (min_delay, max_delay)

    def connect_async(self, host, port, keepalive):
        """Record an async connect request."""

        self.connect_args = (host, port, keepalive)

    def loop_start(self):
        """Pretend to start the paho network loop."""

    def loop_stop(self):
        """Pretend to stop the paho network loop."""

    def disconnect(self):
        """Pretend to disconnect."""

    def publish(self, topic, payload=None, retain=False):
        """Record published MQTT messages."""

        self.published.append((topic, payload, retain))

    def subscribe(self, topic):
        """Record MQTT subscriptions."""

        self.subscriptions.append(topic)


def fake_mqtt_client(service: GrowattMqttService) -> FakeMqttClient:
    """Return the patched MQTT client with its test-only recording attributes."""

    if TYPE_CHECKING:
        return FakeMqttClient()
    return getattr(service, "_client")


class FakeMqttMessage:  # pylint: disable=too-few-public-methods
    """Small paho-like MQTT message."""

    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload.encode("utf-8")


def make_mqtt_config(**overrides):
    """Build a complete MQTT config for service tests."""

    config = {
        "host": "mqtt.local",
        "port": 1883,
        "username": None,
        "password": None,
        "client_id": "growatt-test",
        "keepalive": 60,
        "topic_prefix": "growatt/spf5000es",
        "discovery_prefix": "homeassistant",
        "device_id": "growatt_spf5000es",
        "device_name": "Growatt SPF 5000 ES",
        "retain": True,
        "config_interval_sec": 300,
    }
    config.update(overrides)
    return GrowattMqttConfig(**config)


def make_mqtt_service(inverter=None, scheduler=None, **config_overrides):
    """Build a GrowattMqttService wired to fakes for tests."""

    if inverter is None:
        inverter = Mock()
        inverter.consecutive_read_failures = 0
        inverter.has_pending_readback = False
    with patch("growatt.mqtt.Client", FakeMqttClient):
        return GrowattMqttService(
            inverter,
            make_mqtt_config(**config_overrides),
            scheduler if scheduler is not None else Scheduler(clock=FakeClock()),
        )


class GrowattRecoveryTest(unittest.TestCase):  # pylint: disable=too-many-public-methods
    """Regression tests for Modbus recovery paths."""

    def test_read_retries_after_empty_response(self):
        """An empty response should reopen the port and retry the transaction."""

        client = GrowattModbusClient("/dev/null", retries=1, reconnect_delay_sec=0)
        fake = FakeSerialClient([None, ReadResponse([1, 2])])
        client._client = fake  # pylint: disable=protected-access

        self.assertEqual(client.read_input_registers(0, 2), [1, 2])
        self.assertEqual(fake.closed, 1)
        self.assertEqual(fake.connected, 1)

    def test_modbus_register_limits_are_enforced(self):
        """Read validation should cap requests to the inverter-safe size."""

        client = GrowattModbusClient("/dev/null", reconnect_delay_sec=0)

        # pylint: disable=protected-access
        client._validate_read_register_range(0, 45)
        client._validate_write_register_range(0, 123)

        with self.assertRaises(ValueError):
            client._validate_read_register_range(0, 46)
        with self.assertRaises(ValueError):
            client._validate_write_register_range(0, 124)

    def test_modbus_operations_wait_after_config_write_defer(self):
        """Any Modbus operation after a config write should honor the settle delay."""

        client = GrowattModbusClient("/dev/null", reconnect_delay_sec=0)
        fake = FakeSerialClient([ReadResponse([1])])
        client._client = fake  # pylint: disable=protected-access

        with (
            patch("growatt.perf_counter", side_effect=[100.0, 100.25]),
            patch("growatt.sleep") as sleep_mock,
        ):
            client.defer_next_operations(0.85)
            self.assertEqual(client.read_input_registers(0, 1), [1])

        sleep_mock.assert_called_once()
        self.assertAlmostEqual(sleep_mock.call_args.args[0], 0.6)

    def test_register_read_windows_use_inverter_safe_spans(self):
        """Status and config reads should avoid oversized inverter requests."""

        self.assertEqual(
            INPUT_REGISTER_WINDOWS,
            [(0, 45), (45, 44)],
        )
        self.assertEqual(
            HOLDING_REGISTER_WINDOWS,
            [(0, 45), (45, 45), (90, 18)],
        )

    def test_unknown_register_enum_is_skipped(self):
        """One undecodable value must not abort the whole status publish."""

        with self.assertLogs("growatt", level="WARNING"):
            decoded = GrowattInverter._decode_register_table(  # pylint: disable=protected-access
                [999], {"OnOff": (0, 1, RegType.UINT, ON_OFF_R.__getitem__, None)}
            )
        self.assertEqual(decoded, {})

    def test_unknown_system_status_falls_back_to_raw_value(self):
        """Newer firmware status codes should still publish a readable value."""

        postprocess = INPUT_REGISTERS["SystemStatus"][3]
        self.assertEqual(postprocess(999), "Unknown (999)")

    def test_char_registers_tolerate_invalid_utf8(self):
        """Text-like device registers may contain arbitrary non-UTF-8 bytes."""

        self.assertEqual(
            GrowattInverter.registers_to_char([0x4142, 0xDD00, 0x0000], 0, 3),
            "AB�",
        )

    def test_legacy_config_uses_runtime_fallbacks(self):
        """Older configs that omit newer optional keys should still load."""

        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as config_file:
            config_file.write(
                textwrap.dedent(
                    """
                    [MODBUS]
                    PORT = /dev/ttyUSB0
                    """
                )
            )
            config_file.flush()

            config = read_app_config(config_file.name)

        self.assertEqual(config.modbus.timeout_sec, 1.5)
        self.assertEqual(config.modbus.retries, 2)
        self.assertEqual(config.modbus.write_batch_delay_sec, 0.25)
        self.assertEqual(config.mqtt.topic_prefix, "growatt_spf5000es")
        self.assertIsNone(config.mqtt.username)
        self.assertEqual(config.mqtt.config_interval_sec, 1800.0)

    def test_mqtt_discovery_exposes_writable_selects(self):
        """Home Assistant discovery should expose enum settings as selects."""

        service = make_mqtt_service()

        client = fake_mqtt_client(service)
        service._on_connect(client, None, None, 0)  # pylint: disable=protected-access

        self.assertIn(
            "growatt/spf5000es/config/+/set",
            client.subscriptions,
        )
        messages = {topic: payload for topic, payload, _retain in client.published}
        topic = (
            "homeassistant/select/growatt_spf5000es/"
            "growatt_spf5000es_output_config/config"
        )
        payload = json.loads(messages[topic])

        self.assertEqual(
            payload["command_topic"],
            "growatt/spf5000es/config/output_config/set",
        )
        self.assertEqual(
            payload["state_topic"],
            "growatt/spf5000es/config/output_config/state",
        )
        self.assertEqual(payload["options"], ["SBU", "SOL", "UTI", "SUB"])
        self.assertEqual(payload["icon"], "mdi:transmission-tower-export")
        self.assertEqual(payload["device"]["identifiers"], ["growatt_spf5000es"])

        number_topic = (
            "homeassistant/number/growatt_spf5000es/"
            "growatt_spf5000es_max_charge_amps/config"
        )
        number_payload = json.loads(messages[number_topic])

        self.assertEqual(number_payload["unit_of_measurement"], "A")
        self.assertEqual(number_payload["device_class"], "current")
        self.assertEqual(number_payload["icon"], "mdi:current-ac")

        button_topic = (
            "homeassistant/button/growatt_spf5000es/growatt_spf5000es_sync_time/config"
        )
        button_payload = json.loads(messages[button_topic])
        self.assertEqual(button_payload["icon"], "mdi:clock-sync-outline")

    def test_mqtt_discovery_sets_sys_year_number_limits(self):
        """Clock number entities should accept real year values in Home Assistant."""

        service = make_mqtt_service()

        client = fake_mqtt_client(service)
        service._on_connect(client, None, None, 0)  # pylint: disable=protected-access

        messages = {topic: payload for topic, payload, _retain in client.published}
        topic = (
            "homeassistant/number/growatt_spf5000es/growatt_spf5000es_sys_year/config"
        )
        payload = json.loads(messages[topic])

        self.assertEqual(
            payload["state_topic"], "growatt/spf5000es/config/sys_year/state"
        )
        self.assertEqual(
            payload["command_topic"], "growatt/spf5000es/config/sys_year/set"
        )
        self.assertEqual(payload["min"], 2000)
        self.assertEqual(payload["max"], 2099)
        self.assertEqual(payload["step"], 1)

    def test_mqtt_discovery_sets_generic_number_limits(self):
        """Generic numeric config entities should accept full UINT register values."""

        service = make_mqtt_service()

        client = fake_mqtt_client(service)
        service._on_connect(client, None, None, 0)  # pylint: disable=protected-access

        messages = {topic: payload for topic, payload, _retain in client.published}
        for slug in ("flash_start", "function_mask", "uw_bat_piece_num"):
            topic = (
                "homeassistant/number/growatt_spf5000es/"
                f"growatt_spf5000es_{slug}/config"
            )
            payload = json.loads(messages[topic])
            self.assertEqual(payload["min"], 0)
            self.assertEqual(payload["max"], 65535)
            self.assertEqual(payload["step"], 1)

    def test_mqtt_discovery_exposes_read_only_boolean_as_binary_sensor(self):
        """Boolean config states should not be discovered as numeric sensors."""

        service = make_mqtt_service()

        client = fake_mqtt_client(service)
        service._on_connect(client, None, None, 0)  # pylint: disable=protected-access

        messages = {topic: payload for topic, payload, _retain in client.published}
        for slug in ("debug_mode_enable",):
            topic = (
                "homeassistant/binary_sensor/growatt_spf5000es/"
                f"growatt_spf5000es_{slug}/config"
            )
            payload = json.loads(messages[topic])
            self.assertEqual(
                payload["state_topic"], f"growatt/spf5000es/config/{slug}/state"
            )
            self.assertEqual(payload["payload_on"], "true")
            self.assertEqual(payload["payload_off"], "false")
            self.assertNotIn("device_class", payload)
            self.assertNotIn("state_class", payload)
            self.assertNotIn("unit_of_measurement", payload)

            stale_sensor_topic = (
                "homeassistant/sensor/growatt_spf5000es/"
                f"growatt_spf5000es_{slug}/config"
            )
            self.assertEqual(messages[stale_sensor_topic], "")

    def test_mqtt_discovery_sets_energy_state_class_total_increasing(self):
        """Energy sensors should use a Home Assistant-compatible state class."""

        service = make_mqtt_service()

        client = fake_mqtt_client(service)
        service._on_connect(client, None, None, 0)  # pylint: disable=protected-access

        messages = {topic: payload for topic, payload, _retain in client.published}
        for slug in ("pv2_energy_todayk_wh", "pv2_energy_totalk_wh"):
            topic = (
                "homeassistant/sensor/growatt_spf5000es/"
                f"growatt_spf5000es_{slug}/config"
            )
            payload = json.loads(messages[topic])
            self.assertEqual(payload["device_class"], "energy")
            self.assertEqual(payload["unit_of_measurement"], "kWh")
            self.assertEqual(payload["state_class"], "total_increasing")

    def test_mqtt_discovery_sets_battery_soc_as_percent(self):
        """BatterySOC should not be mistaken for a Celsius temperature sensor."""

        service = make_mqtt_service()

        client = fake_mqtt_client(service)
        service._on_connect(client, None, None, 0)  # pylint: disable=protected-access

        messages = {topic: payload for topic, payload, _retain in client.published}
        topic = (
            "homeassistant/sensor/growatt_spf5000es/"
            "growatt_spf5000es_battery_soc/config"
        )
        payload = json.loads(messages[topic])

        self.assertEqual(payload["unit_of_measurement"], "%")
        self.assertEqual(payload["icon"], "mdi:percent-outline")
        self.assertNotEqual(payload.get("device_class"), "temperature")

    def test_mqtt_metadata_prefers_specific_unit_suffixes(self):
        """Specific unit suffixes should win over earlier words in register names."""

        cases = {
            "GridHighVoltLoadReductionWatt1": ("W", "power"),
            "VoltLowLossPercent1": ("%", None),
            "VoltHighLossPercent3": ("%", None),
            "VoltLowLossTime1": ("s", "duration"),
            "FreqReconnectTime": ("s", "duration"),
            "PVLowLimitWattkW": ("kW", "power"),
        }
        for key, (unit, device_class) in cases.items():
            with self.subTest(key=key):
                metadata = GrowattMqttService._sensor_metadata(  # pylint: disable=protected-access
                    key
                )
                self.assertEqual(metadata["unit_of_measurement"], unit)
                self.assertEqual(metadata.get("device_class"), device_class)

    def test_mqtt_command_is_handled_on_loop_thread(self):
        """Commands must be queued by paho callbacks and run by the scheduler."""

        inverter = Mock()
        inverter.consecutive_read_failures = 0
        inverter.has_pending_readback = False
        scheduler = Scheduler(clock=FakeClock())
        service = make_mqtt_service(inverter=inverter, scheduler=scheduler)

        service._on_message(  # pylint: disable=protected-access
            service._client,  # pylint: disable=protected-access
            None,
            FakeMqttMessage("growatt/spf5000es/config/output_config/set", "SBU"),
        )
        inverter.write_config.assert_not_called()

        scheduler.run_pending()
        inverter.write_config.assert_called_once_with("OutputConfig", "SBU")

    def test_accepted_command_publishes_optimistic_state_echo(self):
        """The UI must see the expected value as soon as a write is accepted."""

        inverter = Mock()
        inverter.consecutive_read_failures = 0
        inverter.has_pending_readback = False
        inverter.write_config.return_value = 30
        scheduler = Scheduler(clock=FakeClock())
        service = make_mqtt_service(inverter=inverter, scheduler=scheduler)

        service._on_message(  # pylint: disable=protected-access
            service._client,  # pylint: disable=protected-access
            None,
            FakeMqttMessage("growatt/spf5000es/config/max_charge_amps/set", "30"),
        )
        scheduler.run_pending()

        self.assertIn(
            ("growatt/spf5000es/config/max_charge_amps/state", "30", True),
            fake_mqtt_client(service).published,
        )

    def test_config_publish_rearms_while_readback_pending(self):
        """Config publishes must retry soon while a write awaits read-back."""

        inverter = Mock()
        inverter.consecutive_read_failures = 0
        inverter.has_pending_readback = True
        inverter.read_config.return_value = {}
        scheduler = Scheduler(clock=FakeClock())
        service = make_mqtt_service(inverter=inverter, scheduler=scheduler)
        service._connected = True  # pylint: disable=protected-access

        scheduler.schedule(TASK_MQTT_CONFIG, 0.0)
        scheduler.run_pending()

        self.assertEqual(
            scheduler.next_timeout(), GrowattMqttService.CONFIG_VERIFY_RETRY_SEC
        )

    def test_time_sync_command_runs_via_command_queue(self):
        """The HA sync-time button must trigger sync on the loop thread."""

        inverter = Mock()
        inverter.consecutive_read_failures = 0
        inverter.has_pending_readback = False
        scheduler = Scheduler(clock=FakeClock())
        service = make_mqtt_service(inverter=inverter, scheduler=scheduler)

        service._on_message(  # pylint: disable=protected-access
            service._client,  # pylint: disable=protected-access
            None,
            FakeMqttMessage("growatt/spf5000es/time_sync/set", "sync"),
        )
        inverter.sync_time.assert_not_called()

        scheduler.run_pending()
        inverter.sync_time.assert_called_once()

    def test_mqtt_command_queue_is_bounded(self):
        """A hostile or looping publisher must not grow memory without bound."""

        service = make_mqtt_service()
        for _ in range(GrowattMqttService.COMMAND_QUEUE_MAX + 5):
            service._on_message(  # pylint: disable=protected-access
                service._client,  # pylint: disable=protected-access
                None,
                FakeMqttMessage("growatt/spf5000es/config/output_config/set", "SBU"),
            )

        self.assertEqual(
            len(service._commands),  # pylint: disable=protected-access
            GrowattMqttService.COMMAND_QUEUE_MAX,
        )

    def test_mqtt_client_limits_offline_publish_queue(self):
        """The MQTT client should not allow a large reconnect publish burst."""

        service = make_mqtt_service()

        self.assertEqual(fake_mqtt_client(service).max_queued_messages, 1)

    def test_mqtt_start_uses_async_connect_with_reconnect_backoff(self):
        """A down broker at startup must not crash the process."""

        service = make_mqtt_service()
        service.start()

        client = fake_mqtt_client(service)
        self.assertEqual(client.connect_args, ("mqtt.local", 1883, 60))
        self.assertEqual(client.reconnect_delay, (1, 30))

    def test_write_queue_size_is_enforced_without_queue_thread(self):
        """The event-loop write queue should still apply backpressure."""

        inverter = GrowattInverter(
            make_modbus_config(write_queue_size=1, write_batch_delay_sec=60),
            Scheduler(clock=FakeClock()),
        )

        inverter.write_config("MaxChargeAmps", 30)
        with self.assertRaises(WriteQueueFullError):
            inverter.write_config("ACChargeAmps", 20)

    def test_sync_time_waits_for_config_settle_before_second_boundary(self):
        """Clock sync should settle first, then choose the exact second to write."""

        inverter = GrowattInverter(make_modbus_config(), Scheduler(clock=FakeClock()))
        fake = FakeGrowattClient()
        inverter.client = fake

        with (
            patch("growatt.sleep") as sleep_mock,
            patch("growatt.datetime") as datetime_mock,
        ):
            datetime_mock.now.side_effect = [
                datetime(2026, 5, 17, 12, 34, 56, 200000),
                datetime(2026, 5, 17, 12, 34, 57),
            ]
            self.assertEqual(
                inverter.sync_time(),
                [2026, 5, 17, 12, 34, 57],
            )

        self.assertEqual(fake.ready_waits, 1)
        sleep_mock.assert_called_once_with(0.8)
        self.assertEqual(fake.writes, [(45, [2026, 5, 17, 12, 34, 57])])

    def test_queued_write_flushes_when_due(self):
        """Queued writes should flush once the batch window elapses."""

        clock = FakeClock()
        scheduler = Scheduler(clock=clock)
        inverter = GrowattInverter(
            make_modbus_config(write_batch_delay_sec=60), scheduler
        )
        fake = FakeGrowattClient()
        inverter.client = fake

        inverter.write_config("MaxChargeAmps", 30)
        scheduler.run_pending()
        self.assertEqual(fake.writes, [])

        clock.advance(60.0)
        scheduler.run_pending()
        self.assertEqual(fake.writes, [(34, [30])])
        self.assertEqual(
            fake.deferred, [GrowattInverter.CONFIG_WRITE_SETTLE_DELAY_SEC]
        )

    def test_new_writes_do_not_delay_a_pending_flush(self):
        """A second queued write must not push back the armed flush deadline."""

        clock = FakeClock()
        scheduler = Scheduler(clock=clock)
        inverter = GrowattInverter(
            make_modbus_config(write_batch_delay_sec=60), scheduler
        )
        fake = FakeGrowattClient()
        inverter.client = fake

        inverter.write_config("MaxChargeAmps", 30)
        clock.advance(30.0)
        inverter.write_config("ACChargeAmps", 20)
        clock.advance(30.0)
        scheduler.run_pending()

        self.assertEqual(fake.writes, [(34, [30]), (38, [20])])

    def test_write_config_returns_expected_readback_value(self):
        """write_config must report the value a verified read will show."""

        inverter = GrowattInverter(
            make_modbus_config(), Scheduler(clock=FakeClock())
        )
        inverter.client = FakeGrowattClient()

        self.assertEqual(inverter.write_config("MaxChargeAmps", 30), 30)
        self.assertEqual(inverter.write_config("BatLowtoUti", 46), 46.0)

    def test_stale_readback_is_suppressed_until_write_is_visible(self):
        """A pre-write device value must never be reported after a write."""

        clock = FakeClock()
        scheduler = Scheduler(clock=clock)
        inverter = GrowattInverter(make_modbus_config(), scheduler)
        fake = FakeGrowattClient()
        inverter.client = fake
        fake.holding[34] = 25  # device still reports the old value

        inverter.write_config("MaxChargeAmps", 30)
        clock.advance(0.25)
        scheduler.run_pending()  # flush writes; device read-back still stale
        self.assertEqual(fake.writes, [(34, [30])])

        self.assertNotIn("MaxChargeAmps", inverter.read_config())
        self.assertTrue(inverter.has_pending_readback)

        fake.apply_writes()  # device finally reflects the write
        self.assertEqual(inverter.read_config()["MaxChargeAmps"], 30)
        self.assertFalse(inverter.has_pending_readback)

    def test_stale_readback_gives_up_after_grace_period(self):
        """Suppression must not hide a key forever if the device never agrees."""

        clock = FakeClock()
        scheduler = Scheduler(clock=clock)
        inverter = GrowattInverter(make_modbus_config(), scheduler)
        fake = FakeGrowattClient()
        inverter.client = fake
        fake.holding[34] = 25

        inverter.write_config("MaxChargeAmps", 30)
        clock.advance(0.25)
        scheduler.run_pending()
        self.assertNotIn("MaxChargeAmps", inverter.read_config())

        clock.advance(GrowattInverter.READBACK_GRACE_SEC)
        self.assertEqual(inverter.read_config()["MaxChargeAmps"], 25)
        self.assertFalse(inverter.has_pending_readback)

    def test_unflushed_write_already_suppresses_stale_reads(self):
        """Config reads between queue and flush must not report old values."""

        clock = FakeClock()
        scheduler = Scheduler(clock=clock)
        inverter = GrowattInverter(make_modbus_config(), scheduler)
        fake = FakeGrowattClient()
        inverter.client = fake
        fake.holding[34] = 25

        inverter.write_config("MaxChargeAmps", 30)
        self.assertNotIn("MaxChargeAmps", inverter.read_config())

    def test_time_sync_runs_when_due_and_reschedules(self):
        """The scheduled time sync should write the clock and re-arm itself."""

        clock = FakeClock()
        scheduler = Scheduler(clock=clock)
        inverter = GrowattInverter(make_modbus_config(), scheduler)
        fake = FakeGrowattClient()
        inverter.client = fake

        scheduler.schedule(TASK_TIME_SYNC, 0.0)
        with (
            patch("growatt.sleep"),
            patch("growatt.datetime") as datetime_mock,
        ):
            datetime_mock.now.return_value = datetime(2026, 5, 17, 12, 34, 56)
            scheduler.run_pending()

        self.assertEqual(fake.writes, [(45, [2026, 5, 17, 12, 34, 56])])
        self.assertEqual(fake.ready_waits, 1)
        self.assertEqual(scheduler.next_timeout(), 720.0)

    def test_inverter_tracks_consecutive_read_failures(self):
        """Failed status reads should count up; a success should reset."""

        scheduler = Scheduler(clock=FakeClock())
        inverter = GrowattInverter(make_modbus_config(), scheduler)
        fake = FakeGrowattClient()
        inverter.client = fake

        fake.fail_reads = True
        for expected in (1, 2):
            with self.assertRaises(ModbusException):
                inverter.read_status()
            self.assertEqual(inverter.consecutive_read_failures, expected)

        fake.fail_reads = False
        inverter.read_status()
        self.assertEqual(inverter.consecutive_read_failures, 0)

    def test_mqtt_publishes_skip_while_disconnected(self):
        """Armed publish tasks must be no-ops while the broker is down."""

        inverter = Mock()
        inverter.consecutive_read_failures = 0
        inverter.has_pending_readback = False
        scheduler = Scheduler(clock=FakeClock())
        service = make_mqtt_service(inverter=inverter, scheduler=scheduler)
        scheduler.schedule(TASK_MQTT_STATUS, 0.0)
        scheduler.schedule(TASK_MQTT_CONFIG, 0.0)

        scheduler.run_pending()

        inverter.read_status.assert_not_called()
        inverter.read_config.assert_not_called()
        self.assertEqual(fake_mqtt_client(service).published, [])

    def test_config_publish_runs_before_status_when_both_due(self):
        """Config readback keeps priority over the 1 s status poll."""

        inverter = Mock()
        inverter.consecutive_read_failures = 0
        inverter.has_pending_readback = False
        inverter.read_status.return_value = {"SystemStatus": "Standby"}
        inverter.read_config.return_value = {"OutputConfig": "SBU"}
        scheduler = Scheduler(clock=FakeClock())
        service = make_mqtt_service(inverter=inverter, scheduler=scheduler)
        service._connected = True  # pylint: disable=protected-access
        scheduler.schedule(TASK_MQTT_STATUS, 0.0)
        scheduler.schedule(TASK_MQTT_CONFIG, 0.0)

        scheduler.run_pending()

        topics = [
            topic
            for topic, _payload, _retain in fake_mqtt_client(service).published
            if topic != "growatt/spf5000es/inverter/availability"
        ]
        self.assertEqual(
            topics,
            [
                "growatt/spf5000es/config/output_config/state",
                "growatt/spf5000es/status/system_status/state",
            ],
        )

    def test_status_publish_repeats_on_interval(self):
        """A successful status publish should re-arm one second out."""

        inverter = Mock()
        inverter.consecutive_read_failures = 0
        inverter.has_pending_readback = False
        inverter.read_status.return_value = {"SystemStatus": "Standby"}
        scheduler = Scheduler(clock=FakeClock())
        service = make_mqtt_service(inverter=inverter, scheduler=scheduler)
        service._connected = True  # pylint: disable=protected-access
        scheduler.schedule(TASK_MQTT_STATUS, 0.0)

        scheduler.run_pending()

        self.assertEqual(scheduler.next_timeout(), 1.0)

    def test_read_failures_toggle_inverter_availability(self):
        """Three failed reads mark the inverter offline; recovery flips it back."""

        inverter = Mock()
        inverter.consecutive_read_failures = 0
        inverter.has_pending_readback = False
        inverter.read_status.side_effect = ModbusException("boom")
        scheduler = Scheduler(clock=FakeClock())
        service = make_mqtt_service(inverter=inverter, scheduler=scheduler)
        service._connected = True  # pylint: disable=protected-access

        service.publish_status()
        inverter.consecutive_read_failures = 3
        service.publish_status()

        inverter.read_status.side_effect = None
        inverter.read_status.return_value = {"SystemStatus": "Standby"}
        inverter.consecutive_read_failures = 0
        inverter.has_pending_readback = False
        service.publish_status()

        topic = "growatt/spf5000es/inverter/availability"
        availability = [
            item for item in fake_mqtt_client(service).published if item[0] == topic
        ]
        self.assertEqual(
            availability,
            [(topic, "online", True), (topic, "offline", True), (topic, "online", True)],
        )

    def test_on_connect_does_not_touch_inverter_lock(self):
        """The paho thread must not read inverter state that needs its I/O lock."""

        class _LockGuardedInverter:  # pylint: disable=too-few-public-methods
            """Stand-in inverter whose guarded property must never be read here."""

            @property
            def consecutive_read_failures(self):
                """Fail the test if the paho thread reads this lock-guarded value."""

                raise AssertionError(
                    "_on_connect must not access consecutive_read_failures"
                )

        service = make_mqtt_service(inverter=_LockGuardedInverter())
        client = fake_mqtt_client(service)

        service._on_connect(client, None, None, 0)  # pylint: disable=protected-access

        topic = "growatt/spf5000es/inverter/availability"
        availability = [item for item in client.published if item[0] == topic]
        self.assertEqual(availability, [])

    def test_discovery_entities_require_broker_and_inverter_availability(self):
        """Entities must go unavailable when either link is down."""

        service = make_mqtt_service()
        client = fake_mqtt_client(service)
        service._on_connect(client, None, None, 0)  # pylint: disable=protected-access

        messages = {topic: payload for topic, payload, _retain in client.published}
        topic = (
            "homeassistant/sensor/growatt_spf5000es/"
            "growatt_spf5000es_battery_soc/config"
        )
        payload = json.loads(messages[topic])

        self.assertEqual(payload["availability_mode"], "all")
        self.assertEqual(
            payload["availability"],
            [
                {"topic": "growatt/spf5000es/availability"},
                {"topic": "growatt/spf5000es/inverter/availability"},
            ],
        )
        self.assertNotIn("availability_topic", payload)

    def test_connect_failure_is_tolerated_and_time_sync_still_armed(self):
        """A missing serial device at boot must not crash the service."""

        scheduler = Scheduler(clock=FakeClock())
        inverter = GrowattInverter(make_modbus_config(), scheduler)
        failing_client = Mock()
        failing_client.connect.side_effect = ModbusException("no such port")
        inverter.client = failing_client

        inverter.connect()

        self.assertEqual(scheduler.next_timeout(), 0.0)


class SchedulerTest(unittest.TestCase):
    """Tests for the cooperative deadline scheduler."""

    def test_registered_task_stays_idle_until_scheduled(self):
        """register() must not arm a task; schedule() must."""

        clock = FakeClock()
        scheduler = Scheduler(clock=clock)
        runs = []
        scheduler.register("task", lambda: runs.append("task"))

        scheduler.run_pending()
        self.assertEqual(runs, [])
        self.assertIsNone(scheduler.next_timeout())

        scheduler.schedule("task", 5.0)
        self.assertEqual(scheduler.next_timeout(), 5.0)
        scheduler.run_pending()
        self.assertEqual(runs, [])

        clock.advance(5.0)
        scheduler.run_pending()
        self.assertEqual(runs, ["task"])
        self.assertIsNone(scheduler.next_timeout())

    def test_due_tasks_run_in_priority_order(self):
        """When several tasks are due, lower priority number runs first."""

        clock = FakeClock()
        scheduler = Scheduler(clock=clock)
        runs = []
        scheduler.register("late", lambda: runs.append("late"), priority=50)
        scheduler.register("early", lambda: runs.append("early"), priority=10)
        scheduler.schedule("late", 0.0)
        scheduler.schedule("early", 0.0)

        scheduler.run_pending()

        self.assertEqual(runs, ["early", "late"])

    def test_schedule_without_replace_keeps_existing_deadline(self):
        """replace=False must not push back an already-armed task."""

        clock = FakeClock()
        scheduler = Scheduler(clock=clock)
        scheduler.register("task", lambda: None)

        scheduler.schedule("task", 1.0)
        scheduler.schedule("task", 60.0, replace=False)
        self.assertEqual(scheduler.next_timeout(), 1.0)

        scheduler.schedule("task", 60.0)
        self.assertEqual(scheduler.next_timeout(), 60.0)

    def test_periodic_task_reschedules_after_run(self):
        """Tasks with an interval re-arm themselves after each run."""

        clock = FakeClock()
        scheduler = Scheduler(clock=clock)
        runs = []
        scheduler.register("task", lambda: runs.append("run"), interval_sec=10.0)
        scheduler.schedule("task", 0.0)

        scheduler.run_pending()

        self.assertEqual(runs, ["run"])
        self.assertEqual(scheduler.next_timeout(), 10.0)

    def test_failing_periodic_task_is_logged_and_rescheduled(self):
        """A raising callback must not kill the loop or drop the task."""

        clock = FakeClock()
        scheduler = Scheduler(clock=clock)

        def boom():
            raise RuntimeError("boom")

        scheduler.register("task", boom, interval_sec=10.0)
        scheduler.schedule("task", 0.0)

        with self.assertLogs("growatt", level="ERROR"):
            scheduler.run_pending()

        self.assertEqual(scheduler.next_timeout(), 10.0)

    def test_callback_reschedule_overrides_interval(self):
        """A callback that reschedules itself wins over the default interval."""

        clock = FakeClock()
        scheduler = Scheduler(clock=clock)
        scheduler.register(
            "task", lambda: scheduler.schedule("task", 3.0), interval_sec=10.0
        )
        scheduler.schedule("task", 0.0)

        scheduler.run_pending()

        self.assertEqual(scheduler.next_timeout(), 3.0)

    def test_cancel_disarms_task(self):
        """cancel() must clear an armed deadline."""

        clock = FakeClock()
        scheduler = Scheduler(clock=clock)
        scheduler.register("task", lambda: None)
        scheduler.schedule("task", 1.0)

        scheduler.cancel("task")

        self.assertIsNone(scheduler.next_timeout())

    def test_schedule_unknown_task_raises(self):
        """Arming an unregistered task is a programming error."""

        scheduler = Scheduler(clock=FakeClock())
        with self.assertRaises(KeyError):
            scheduler.schedule("nope")

    def test_schedule_wakes_waiting_loop(self):
        """schedule() must interrupt wait() so cross-thread work runs promptly."""

        from time import perf_counter as real_clock  # pylint: disable=import-outside-toplevel

        scheduler = Scheduler()
        scheduler.register("task", lambda: None)
        scheduler.schedule("task", 30.0)

        started = real_clock()
        scheduler.wait(5.0)
        self.assertLess(real_clock() - started, 1.0)

    def test_status_poll_backs_off_while_inverter_unreachable(self):
        """Consecutive read failures should stretch the poll interval to 30 s."""

        inverter = Mock()
        inverter.consecutive_read_failures = 0
        inverter.has_pending_readback = False
        inverter.read_status.side_effect = ModbusException("boom")
        clock = FakeClock()
        scheduler = Scheduler(clock=clock)
        service = make_mqtt_service(inverter=inverter, scheduler=scheduler)
        service._connected = True  # pylint: disable=protected-access

        scheduler.schedule(TASK_MQTT_STATUS, 0.0)
        for expected in (2.0, 4.0, 8.0, 16.0, 30.0, 30.0):
            scheduler.run_pending()
            self.assertEqual(scheduler.next_timeout(), expected)
            clock.advance(expected)

        inverter.read_status.side_effect = None
        inverter.read_status.return_value = {"SystemStatus": "Standby"}
        scheduler.run_pending()
        self.assertEqual(scheduler.next_timeout(), 1.0)

    def test_config_publish_retries_soon_after_failure(self):
        """A failed config read must not wait out the 5+ minute interval."""

        inverter = Mock()
        inverter.consecutive_read_failures = 0
        inverter.has_pending_readback = False
        inverter.read_config.side_effect = ModbusException("boom")
        scheduler = Scheduler(clock=FakeClock())
        service = make_mqtt_service(inverter=inverter, scheduler=scheduler)
        service._connected = True  # pylint: disable=protected-access

        scheduler.schedule(TASK_MQTT_CONFIG, 0.0)
        scheduler.run_pending()

        self.assertEqual(scheduler.next_timeout(), 30.0)


if __name__ == "__main__":
    unittest.main()
