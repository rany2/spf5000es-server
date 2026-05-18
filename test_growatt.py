#!/usr/bin/env python3

"""Tests for Modbus validation and recovery behavior."""

import base64
import json
from datetime import datetime
from http.client import HTTPConnection
from http import HTTPStatus
import socket
import tempfile
import threading
import textwrap
import unittest
from unittest.mock import Mock, patch

from pymodbus.exceptions import ModbusException

from growatt import (
    HOLDING_REGISTER_WINDOWS,
    INPUT_REGISTER_WINDOWS,
    MAX_AUTH_HEADER_LENGTH,
    GrowattHTTPAuth,
    GrowattHTTPConfig,
    GrowattHTTPHandler,
    GrowattHTTPServer,
    GrowattInverter,
    GrowattMqttConfig,
    GrowattMqttService,
    GrowattModbusClient,
    ModbusAppConfig,
    WriteQueueFullError,
    growatt_http_handler_factory,
    read_app_config,
)


def make_modbus_config(**overrides):
    """Build a complete Modbus config for inverter tests."""

    config = {
        "port": "/dev/null",
        "write_queue_size": 128,
        "write_batch_delay_sec": 0.05,
        "timeout_sec": 1.5,
        "retries": 2,
        "reconnect_delay_sec": 0.2,
    }
    config.update(overrides)
    return ModbusAppConfig(**config)


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


class FakeGrowattClient:  # pylint: disable=too-few-public-methods
    """Small fake for exercising inverter scheduling without serial hardware."""

    def __init__(self):
        self.writes = []
        self.deferred = []
        self.ready_waits = 0

    def write_registers(self, start, values):
        """Record a Modbus write."""

        self.writes.append((start, values))

    def defer_next_operations(self, delay_sec):
        """Record deferred Modbus timing."""

        self.deferred.append(delay_sec)

    def wait_until_ready_for_operation(self):
        """Record readiness waits."""

        self.ready_waits += 1


class FakeTimeSyncInverter:  # pylint: disable=too-few-public-methods
    """Small fake for exercising the HTTP forced time sync route."""

    def __init__(self, values):
        self.values = values
        self.called = False

    def force_time_sync_now(self):
        """Record and return a forced sync result."""

        self.called = True
        return self.values


class FakeMqttClient:
    """Small fake for exercising MQTT discovery without a broker."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.published = []
        self.subscriptions = []
        self.username = None
        self.password = None
        self.will = None
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None

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


class FakeMqttMessage:  # pylint: disable=too-few-public-methods
    """Small paho-like MQTT message."""

    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload.encode("utf-8")


def build_basic_auth(username="admin", password="admin"):
    """Return a Basic authorization header value for tests."""

    credentials = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode(
        "ascii"
    )
    return f"Basic {credentials}"


def make_mqtt_config(**overrides):
    """Build a complete MQTT config for service tests."""

    config = {
        "enabled": True,
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
        "status_interval_sec": 10,
        "config_interval_sec": 300,
    }
    config.update(overrides)
    return GrowattMqttConfig(**config)


class GrowattRecoveryTest(unittest.TestCase):
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

        self.assertEqual(INPUT_REGISTER_WINDOWS, [(0, 45), (45, 44)])
        self.assertEqual(HOLDING_REGISTER_WINDOWS, [(0, 45), (45, 45), (90, 18)])

    def test_unknown_register_enum_is_modbus_error(self):
        """Unexpected device enum values should be reported as Modbus failures."""

        with self.assertRaises(ModbusException):
            GrowattInverter._postprocess_register_value(  # pylint: disable=protected-access
                "SystemStatus", 999, {0: "Standby"}.__getitem__
            )

    def test_legacy_config_uses_runtime_fallbacks(self):
        """Older configs that omit newer optional keys should still load."""

        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as config_file:
            config_file.write(
                textwrap.dedent(
                    """
                    [MODBUS]
                    PORT = /dev/ttyUSB0

                    [WEB]
                    USER = admin
                    PASS_SALT = salt
                    PASS_HASH = hash
                    """
                )
            )
            config_file.flush()

            config = read_app_config(config_file.name)

        self.assertEqual(config.web.addr, "0.0.0.0")
        self.assertEqual(config.web.port, 8080)
        self.assertEqual(config.web.handler.timeout, 10)
        self.assertEqual(config.web.max_worker_threads, 8)
        self.assertFalse(config.web.handler.x_forwarded_for)
        self.assertEqual(config.modbus.timeout_sec, 1.5)
        self.assertEqual(config.modbus.retries, 2)
        self.assertFalse(config.mqtt.enabled)
        self.assertEqual(config.mqtt.topic_prefix, "growatt_spf5000es")
        self.assertIsNone(config.mqtt.username)

    def test_mqtt_discovery_exposes_writable_selects(self):
        """Home Assistant discovery should expose enum settings as selects."""

        with patch("growatt.mqtt.Client", FakeMqttClient):
            service = GrowattMqttService(Mock(), make_mqtt_config())

        client = service._client  # pylint: disable=protected-access
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
            "homeassistant/button/growatt_spf5000es/"
            "growatt_spf5000es_sync_time/config"
        )
        button_payload = json.loads(messages[button_topic])
        self.assertEqual(button_payload["icon"], "mdi:clock-sync-outline")

    def test_mqtt_command_writes_config_register(self):
        """MQTT config commands should flow through inverter write validation."""

        inverter = Mock()
        with patch("growatt.mqtt.Client", FakeMqttClient):
            service = GrowattMqttService(inverter, make_mqtt_config())

        service._on_message(  # pylint: disable=protected-access
            service._client,  # pylint: disable=protected-access
            None,
            FakeMqttMessage("growatt/spf5000es/config/output_config/set", "SBU"),
        )

        inverter.write_config.assert_called_once_with("OutputConfig", "SBU")

    def test_http_server_serves_second_client_while_first_is_idle(self):
        """An idle TCP client should not monopolize the HTTP accept loop."""

        http_config = GrowattHTTPConfig(
            timeout=2,
            json_indent=None,
            x_forwarded_for=False,
            auth=GrowattHTTPAuth(
                username="admin",
                password_hash=GrowattHTTPHandler.hash_password("admin", "salt"),
                password_salt="salt",
            ),
        )
        server = GrowattHTTPServer(
            ("127.0.0.1", 0),
            growatt_http_handler_factory(
                inverter=Mock(),
                http_config=http_config,
            ),
            max_worker_threads=2,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        idle_client = socket.create_connection(server.server_address, timeout=1)

        try:
            conn = HTTPConnection(*server.server_address, timeout=1)
            conn.request(
                "GET",
                "/",
                headers={"Authorization": build_basic_auth()},
            )
            response = conn.getresponse()
            body = response.read()
            conn.close()

            self.assertEqual(response.status, HTTPStatus.OK)
            self.assertIn(b"<h1>Growatt</h1>", body)
        finally:
            idle_client.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_password_hash_uses_sha1_hmac(self):
        """Password hashing should use the configured SHA1-HMAC format."""

        self.assertEqual(
            GrowattHTTPHandler.hash_password("admin", "salt"),
            "ed5768641a6bdcae1fd5a2b641465e28e5fcea09",
        )

    def test_rejects_oversized_basic_auth_header(self):
        """Oversized Authorization headers should fail before base64 decoding."""

        self.assertFalse(
            GrowattHTTPHandler.validate_basic_auth_header(
                object.__new__(GrowattHTTPHandler),
                "Basic " + ("A" * MAX_AUTH_HEADER_LENGTH),
            )
        )

    def test_run_maintenance_flushes_queued_writes(self):
        """Queued writes should flush from the selector-loop maintenance hook."""

        inverter = GrowattInverter(make_modbus_config(write_batch_delay_sec=60))
        fake = FakeGrowattClient()
        inverter.client = fake

        inverter.write_config("MaxChargeAmps", 30)
        inverter._next_write_flush = 0  # pylint: disable=protected-access
        inverter.run_maintenance()

        self.assertEqual(fake.writes, [(34, [30])])
        self.assertEqual(fake.deferred, [0.85])

    def test_write_queue_size_is_enforced_without_queue_thread(self):
        """The event-loop write queue should still apply backpressure."""

        inverter = GrowattInverter(
            make_modbus_config(write_queue_size=1, write_batch_delay_sec=60)
        )

        inverter.write_config("MaxChargeAmps", 30)
        with self.assertRaises(WriteQueueFullError):
            inverter.write_config("ACChargeAmps", 20)

    def test_run_maintenance_syncs_time_when_due(self):
        """Due clock sync writes should run from the maintenance hook."""

        inverter = GrowattInverter(make_modbus_config())
        fake = FakeGrowattClient()
        inverter.client = fake
        inverter._next_sync_time = 0  # pylint: disable=protected-access

        with patch("growatt.datetime") as datetime_mock:
            datetime_mock.now.return_value = datetime(2026, 5, 17, 12, 34, 56)
            inverter.run_maintenance()

        self.assertEqual(fake.writes, [(45, [2026, 5, 17, 12, 34, 56])])
        self.assertEqual(fake.ready_waits, 1)

    def test_sync_time_waits_for_config_settle_before_second_boundary(self):
        """Clock sync should settle first, then choose the exact second to write."""

        inverter = GrowattInverter(make_modbus_config())
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


if __name__ == "__main__":
    unittest.main()
