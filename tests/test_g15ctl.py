#!/usr/bin/env python3
"""Unit tests for g15ctl.

These cover the logic that is easy to get wrong and dangerous to get wrong:
value conversion, fan curve interpolation and hysteresis, mode alias
resolution, backend capability gating, and the watchdog's escalation path.

Hardware access is faked, so the suite runs as a normal user and on any
machine. Real-hardware verification is tools/verify.sh.

Run: python3 -m pytest tests/ -v    or    python3 tests/test_g15ctl.py
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from g15ctl import backends, constants as C, controller as ctl, curve, state  # noqa: E402


# ---------------------------------------------------------------------------
# Value conversion
# ---------------------------------------------------------------------------


class TestConversion(unittest.TestCase):
    def test_percent_to_boost_endpoints(self):
        self.assertEqual(ctl.percent_to_boost(0), 0)
        self.assertEqual(ctl.percent_to_boost(100), 255)

    def test_percent_to_boost_midpoint(self):
        self.assertEqual(ctl.percent_to_boost(50), 128)

    def test_percent_to_boost_clamps(self):
        self.assertEqual(ctl.percent_to_boost(-40), 0)
        self.assertEqual(ctl.percent_to_boost(999), 255)

    def test_boost_percent_roundtrip_is_stable(self):
        # Rounding must not drift when a value is converted back and forth,
        # otherwise the UI would creep when nudging the fan up and down.
        for percent in range(0, 101):
            boost = ctl.percent_to_boost(percent)
            self.assertLessEqual(abs(ctl.boost_to_percent(boost) - percent), 1)

    def test_boost_to_percent_clamps(self):
        self.assertEqual(ctl.boost_to_percent(-5), 0)
        self.assertEqual(ctl.boost_to_percent(1000), 100)


# ---------------------------------------------------------------------------
# Mode aliases
# ---------------------------------------------------------------------------


class TestModeNames(unittest.TestCase):
    def test_canonical_names_pass_through(self):
        for mode in ("balanced", "quiet", "low-power", "g-mode"):
            self.assertEqual(ctl.normalise_mode(mode), mode)

    def test_aliases(self):
        self.assertEqual(ctl.normalise_mode("perf"), "balanced-performance")
        self.assertEqual(ctl.normalise_mode("turbo"), "g-mode")
        self.assertEqual(ctl.normalise_mode("silent"), "quiet")
        self.assertEqual(ctl.normalise_mode("powersave"), "low-power")

    def test_case_and_underscores(self):
        self.assertEqual(ctl.normalise_mode("  G_MODE "), "g-mode")
        self.assertEqual(ctl.normalise_mode("Balanced"), "balanced")

    def test_unknown_is_returned_unchanged(self):
        # The controller validates against the backend, so normalising must
        # not invent a mode here.
        self.assertEqual(ctl.normalise_mode("nonsense"), "nonsense")


# ---------------------------------------------------------------------------
# Fan curve
# ---------------------------------------------------------------------------


class TestCurveValidation(unittest.TestCase):
    def test_rejects_empty(self):
        with self.assertRaises(curve.CurveError):
            curve.validate([])

    def test_rejects_out_of_range_percent(self):
        with self.assertRaises(curve.CurveError):
            curve.validate([[50, 0], [60, 150]])

    def test_rejects_out_of_range_temp(self):
        with self.assertRaises(curve.CurveError):
            curve.validate([[50, 0], [900, 100]])

    def test_rejects_decreasing_percent(self):
        # A curve that slows the fan as things get hotter is always a mistake.
        with self.assertRaises(curve.CurveError):
            curve.validate([[50, 80], [70, 20]])

    def test_rejects_duplicate_temps(self):
        with self.assertRaises(curve.CurveError):
            curve.validate([[60, 10], [60, 40]])

    def test_rejects_malformed_points(self):
        for bad in ([[50]], [["x", "y"]], [50, 60]):
            with self.assertRaises(curve.CurveError):
                curve.validate(bad)

    def test_sorts_points(self):
        self.assertEqual(
            curve.validate([[80, 90], [50, 10]]),
            [(50.0, 10.0), (80.0, 90.0)],
        )

    def test_default_config_curve_is_valid(self):
        # The shipped default must never fail validation.
        curve.validate(state.DEFAULT_CONFIG["curve"]["points"])


class TestCurveEvaluation(unittest.TestCase):
    def setUp(self):
        self.points = curve.validate([[50, 0], [70, 50], [90, 100]])

    def test_clamps_below_and_above(self):
        self.assertEqual(curve.evaluate(self.points, 20), 0)
        self.assertEqual(curve.evaluate(self.points, 120), 100)

    def test_exact_points(self):
        self.assertEqual(curve.evaluate(self.points, 50), 0)
        self.assertEqual(curve.evaluate(self.points, 70), 50)
        self.assertEqual(curve.evaluate(self.points, 90), 100)

    def test_linear_interpolation(self):
        self.assertAlmostEqual(curve.evaluate(self.points, 60), 25.0)
        self.assertAlmostEqual(curve.evaluate(self.points, 80), 75.0)

    def test_monotonic(self):
        previous = -1.0
        for temp in range(0, 120):
            value = curve.evaluate(self.points, temp)
            self.assertGreaterEqual(value, previous)
            previous = value


class TestCurveHysteresis(unittest.TestCase):
    def setUp(self):
        self.c = curve.CurveController([[50, 0], [70, 50], [90, 100]], hysteresis_c=5)

    def test_rises_immediately(self):
        self.assertEqual(self.c.update(50), 0)
        self.assertAlmostEqual(self.c.update(70), 50.0)
        self.assertAlmostEqual(self.c.update(90), 100.0)

    def test_holds_speed_within_hysteresis_band(self):
        self.c.update(80)  # -> 75%
        # 78 C would normally be 70%, but it is inside the 5 C band.
        self.assertAlmostEqual(self.c.update(78), 75.0)

    def test_steps_down_once_clear_of_band(self):
        self.c.update(80)
        self.assertAlmostEqual(self.c.update(70), 50.0)

    def test_reset_clears_state(self):
        self.c.update(90)
        self.c.reset()
        self.assertEqual(self.c.update(50), 0)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class FakeWmax:
    """In-memory stand-in for the firmware, modelled on the real 5530.

    Mirrors what tools/probe3.py and tools/verify.sh measured on BIOS 1.34.0:
    fans 0x32/0x33 at 4800 RPM max, sensors 0x01/0x06, profiles
    0xA0/0xA1/0xA3/0xA5, G-Mode off.

    Importantly, G-Mode here behaves the way the real hardware was measured to
    behave: enabling it does NOT change what 0x14/0x0B reports. Modelling it
    the other way round is what let a real bug through initially.
    """

    RESOURCES = [0x32, 0x33, 0x101, 0x106, 0xA0, 0xA1, 0xA5, 0xA3]

    #: Set True to emulate firmware that reports 0xAB as the current profile.
    reports_gmode_as_profile = False

    def __init__(self):
        self.path = r"\_SB.AMWW.WMAX"
        self.profile = 0xA0
        self.gmode = 0
        self.boost = {0x32: 0, 0x33: 0}
        self.calls: list[tuple] = []

    def call(self, method, op, a1=0, a2=0, a3=0):
        self.calls.append((method, op, a1, a2))
        if method == C.M_THERMAL_INFO:
            if op == C.OP_RESOURCE_ID:
                if a1 < len(self.RESOURCES):
                    return self.RESOURCES[a1]
                return 0xFFFFFFFE
            if op == C.OP_GET_CURRENT_PROFILE:
                if self.gmode and self.reports_gmode_as_profile:
                    return C.PROFILE_GMODE
                return self.profile
            if op == C.OP_GET_RPM:
                return {0x32: 1091, 0x33: 1240}.get(a1, 0xFFFFFFFF)
            if op == C.OP_GET_TEMP:
                return {0x01: 65, 0x06: 53}.get(a1, 0xFFFFFFFF)
            if op == C.OP_GET_FAN_MAX_RPM:
                return 4800 if a1 in self.boost else 0xFFFFFFFF
            if op == C.OP_GET_FAN_BOOST:
                return self.boost.get(a1, 0xFFFFFFFF)
            return 0xFFFFFFFE
        if method == C.M_THERMAL_CONTROL:
            if op == C.OP_ACTIVATE_PROFILE:
                self.profile, self.gmode = a1, 0
                return 0
            if op == C.OP_SET_FAN_BOOST:
                if a1 not in self.boost:
                    return 0xFFFFFFFF
                self.boost[a1] = a2
                return 0
        if method == C.M_GAME_SHIFT:
            if op == C.OP_SET_GAME_SHIFT:
                self.gmode = 1 if a1 else 0
                return 0
            if op == C.OP_GET_GAME_SHIFT:
                return self.gmode
        return 0xFFFFFFFE

    def query(self, method, op, a1=0, a2=0):
        value = self.call(method, op, a1, a2)
        return None if value in C.WMAX_ERRORS else value


class TestAwccBackend(unittest.TestCase):
    def setUp(self):
        self.fw = FakeWmax()
        self.backend = backends.AwccAcpiBackend(self.fw)

    def test_enumerates_real_topology(self):
        self.assertEqual(self.backend.fan_ids, (0x32, 0x33))
        self.assertEqual(self.backend.fan_max_rpm(), {0x32: 4800, 0x33: 4800})

    def test_discovers_only_supported_profiles(self):
        modes = self.backend.list_modes()
        for expected in ("balanced", "balanced-performance", "quiet", "low-power"):
            self.assertIn(expected, modes)
        self.assertIn(C.MODE_GMODE, modes)
        # The 5530 firmware does not implement these, so they must not appear.
        self.assertNotIn("cool", modes)
        self.assertNotIn("performance", modes)

    def test_set_and_get_mode(self):
        self.backend.set_mode("quiet")
        self.assertEqual(self.backend.get_mode(), "quiet")
        self.assertEqual(self.fw.profile, 0xA3)

    def test_rejects_unsupported_profile(self):
        with self.assertRaises(backends.BackendError):
            self.backend.set_mode("cool")

    def test_gmode_roundtrip(self):
        self.assertFalse(self.backend.get_gmode())
        self.backend.set_gmode(True)
        self.assertTrue(self.backend.get_gmode())
        self.assertEqual(self.backend.get_mode(), C.MODE_GMODE)

    def test_gmode_detected_even_when_profile_does_not_report_it(self):
        # Regression, caught by tools/verify.sh on real hardware: BIOS 1.34.0
        # keeps reporting 0xA0 from 0x14/0x0B while Game Shift is on, so
        # get_mode() must consult 0x25/0x02 rather than trusting the profile.
        self.fw.reports_gmode_as_profile = False
        self.backend.set_gmode(True)
        self.assertEqual(self.fw.profile, 0xA0)
        self.assertEqual(self.backend.get_mode(), C.MODE_GMODE)

    def test_gmode_detected_when_firmware_reports_0xab(self):
        # The other firmware behaviour must keep working too.
        self.fw.reports_gmode_as_profile = True
        self.backend.set_gmode(True)
        self.assertEqual(self.backend.get_mode(), C.MODE_GMODE)

    def test_selecting_profile_leaves_gmode(self):
        self.backend.set_gmode(True)
        self.backend.set_mode("balanced")
        self.assertFalse(self.backend.get_gmode())

    def test_fan_boost_applies_to_both_fans(self):
        self.backend.set_fan_boost(128)
        self.assertEqual(self.fw.boost, {0x32: 128, 0x33: 128})

    def test_fan_boost_single_fan(self):
        self.backend.set_fan_boost(200, fan_id=0x33)
        self.assertEqual(self.fw.boost, {0x32: 0, 0x33: 200})

    def test_fan_boost_clamped(self):
        self.backend.set_fan_boost(9999)
        self.assertEqual(self.fw.boost[0x32], 255)
        self.backend.set_fan_boost(-20)
        self.assertEqual(self.fw.boost[0x32], 0)

    def test_reset_clears_boost_and_gmode(self):
        self.backend.set_fan_boost(255)
        self.backend.set_gmode(True)
        self.backend.reset()
        self.assertEqual(self.fw.boost, {0x32: 0, 0x33: 0})
        self.assertFalse(self.backend.get_gmode())

    def test_rejects_out_of_range_bytes(self):
        # The real transport must never emit a buffer byte above 0xFF.
        from g15ctl.acpi import Wmax
        wmax = Wmax(r"\_SB.AMWW.WMAX")
        with self.assertRaises(ValueError):
            wmax.call(C.M_THERMAL_CONTROL, C.OP_SET_FAN_BOOST, 0x32, 300)


class TestCapabilityGating(unittest.TestCase):
    """A backend must refuse what it cannot do, rather than silently no-op."""

    def test_profiles_only_backend_refuses_fan_control(self):
        backend = backends.DellPcBackend.__new__(backends.DellPcBackend)
        backend.path = "/nonexistent"
        backend.caps = frozenset({backends.CAP_PROFILES})
        with self.assertRaises(backends.BackendError):
            backend.set_fan_boost(128)

    def test_profiles_only_backend_refuses_gmode(self):
        backend = backends.DellPcBackend.__new__(backends.DellPcBackend)
        backend.path = "/nonexistent"
        backend.caps = frozenset({backends.CAP_PROFILES})
        with self.assertRaises(backends.BackendError):
            backend.set_gmode(True)

    def test_unknown_backend_name_is_rejected(self):
        with self.assertRaises(backends.BackendError):
            backends.detect("not-a-backend")

    def test_dell_pc_does_not_alias_performance_to_gmode(self):
        # Regression: dell-pc's `performance` is an ordinary profile. Mapping
        # it to g-mode would make the tool claim a capability it lacks.
        self.assertEqual(backends.DellPcBackend.profile_map, {})
        self.assertEqual(
            backends.NativeProfileBackend.profile_map.get("performance"),
            C.MODE_GMODE,
        )


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class TestController(unittest.TestCase):
    def setUp(self):
        self.fw = FakeWmax()
        self.backend = backends.AwccAcpiBackend(self.fw)
        self.controller = ctl.Controller(self.backend, dict(state.DEFAULT_CONFIG))
        # Pretend to be root and keep state writes out of the filesystem.
        self._patches = [
            mock.patch("os.geteuid", return_value=0),
            mock.patch("g15ctl.state.save_state"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_set_mode_accepts_alias(self):
        self.controller.set_mode("silent")
        self.assertEqual(self.backend.get_mode(), "quiet")

    def test_set_mode_rejects_unknown(self):
        with self.assertRaises(ctl.ControlError):
            self.controller.set_mode("ludicrous")

    def test_switching_profile_clears_manual_boost(self):
        # Otherwise a boost set under one profile would silently persist.
        self.controller.set_fan(80)
        self.assertNotEqual(self.fw.boost[0x32], 0)
        self.controller.set_mode("quiet")
        self.assertEqual(self.fw.boost[0x32], 0)

    def test_fan_auto_releases_control(self):
        self.controller.set_fan(90)
        self.controller.set_fan("auto")
        self.assertEqual(self.fw.boost, {0x32: 0, 0x33: 0})
        self.assertEqual(self.controller.fan_mode(), ctl.FAN_AUTO)

    def test_fan_mode_reports_manual(self):
        self.controller.set_fan(50)
        self.assertEqual(self.controller.fan_mode(), ctl.FAN_MANUAL)

    def test_fan_rejects_bad_input(self):
        for bad in ("abc", "-5", "101", "200%"):
            with self.assertRaises(ctl.ControlError):
                self.controller.set_fan(bad)

    def test_fan_accepts_percent_suffix(self):
        self.controller.set_fan("75%")
        self.assertEqual(self.fw.boost[0x32], ctl.percent_to_boost(75))

    def test_reset_returns_to_default(self):
        self.controller.set_fan(100)
        self.controller.set_gmode(True)
        self.controller.reset()
        self.assertEqual(self.fw.boost, {0x32: 0, 0x33: 0})
        self.assertFalse(self.backend.get_gmode())
        self.assertEqual(self.backend.get_mode(), "balanced")

    def test_toggle_gmode(self):
        self.assertTrue(self.controller.toggle_gmode())
        self.assertFalse(self.controller.toggle_gmode())

    def test_status_shape(self):
        data = self.controller.status()
        for key in ("backend", "mode", "fans", "temps", "battery",
                    "capabilities", "available_modes", "fan_control"):
            self.assertIn(key, data)


class TestPrivilegeChecks(unittest.TestCase):
    def setUp(self):
        self.controller = ctl.Controller(
            backends.AwccAcpiBackend(FakeWmax()), dict(state.DEFAULT_CONFIG)
        )

    def test_control_requires_root(self):
        with mock.patch("os.geteuid", return_value=1000):
            for action in (lambda: self.controller.set_mode("quiet"),
                           lambda: self.controller.set_fan(50),
                           lambda: self.controller.set_gmode(True)):
                with self.assertRaises(ctl.ControlError):
                    action()

    def test_status_does_not_require_root(self):
        with mock.patch("os.geteuid", return_value=1000):
            self.controller.status()  # must not raise


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------


class TestWatchdog(unittest.TestCase):
    def setUp(self):
        from g15ctl import daemon
        self.fw = FakeWmax()
        backend = backends.AwccAcpiBackend(self.fw)
        config = dict(state.DEFAULT_CONFIG)
        config["curve"] = dict(config["curve"], enabled=False)
        controller = ctl.Controller(backend, config)
        self.patch = mock.patch("os.geteuid", return_value=0)
        self.patch.start()
        self.daemon = daemon.Daemon(controller)

    def tearDown(self):
        self.patch.stop()

    def test_ignores_brief_spike(self):
        # One hot sample must not trigger; transient spikes are normal.
        self.assertFalse(self.daemon._run_watchdog("CPU", 90))
        self.assertEqual(self.fw.boost[0x32], 0)

    def test_forces_full_boost_after_sustained_heat(self):
        for _ in range(C.WATCHDOG_SAMPLES):
            engaged = self.daemon._run_watchdog("CPU", 90)
        self.assertTrue(engaged)
        self.assertEqual(self.fw.boost[0x32], 255)

    def test_releases_control_at_critical(self):
        self.daemon._last_applied = 128
        for _ in range(C.WATCHDOG_SAMPLES):
            engaged = self.daemon._run_watchdog("CPU", 99)
        self.assertTrue(engaged)
        # Past critical we trust the firmware, not ourselves.
        self.assertEqual(self.fw.boost[0x32], 0)

    def test_stands_down_when_cool(self):
        for _ in range(C.WATCHDOG_SAMPLES):
            self.daemon._run_watchdog("CPU", 90)
        self.assertTrue(self.daemon._watchdog_engaged)
        self.daemon._run_watchdog("CPU", 60)
        self.assertFalse(self.daemon._watchdog_engaged)

    def test_disabled_watchdog_does_nothing(self):
        self.daemon.config["watchdog"] = {"enabled": False}
        for _ in range(10):
            self.assertFalse(self.daemon._run_watchdog("CPU", 99))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestConfig(unittest.TestCase):
    def test_partial_config_inherits_defaults(self):
        with mock.patch("g15ctl.state._read_json",
                        return_value={"default_mode": "quiet"}):
            config = state.load_config()
        self.assertEqual(config["default_mode"], "quiet")
        # A user file that predates a new option must still get the default.
        self.assertIn("watchdog", config)
        self.assertTrue(config["watchdog"]["enabled"])

    def test_nested_override_is_merged_not_replaced(self):
        with mock.patch("g15ctl.state._read_json",
                        return_value={"watchdog": {"critical_c": 80}}):
            config = state.load_config()
        self.assertEqual(config["watchdog"]["critical_c"], 80)
        self.assertIn("force_boost_c", config["watchdog"])

    def test_corrupt_config_falls_back_to_defaults(self):
        with mock.patch("builtins.open", mock.mock_open(read_data="{not json")):
            config = state.load_config()
        self.assertEqual(config["default_mode"],
                         state.DEFAULT_CONFIG["default_mode"])

    def test_shutdown_reset_defaults_on(self):
        # This is what protects a dual-booted Windows/AWCC.
        self.assertTrue(state.DEFAULT_CONFIG["reset_on_shutdown"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
