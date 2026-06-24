from django.test import TestCase
from .calculator import (
    erlang_c, service_level, agents_required,
    parse_aht, calculate_staffing,
)


class ErlangCTests(TestCase):
    def test_overloaded_returns_one(self):
        # When agents ≤ traffic intensity the queue can never clear
        self.assertEqual(erlang_c(agents=2, traffic_intensity=3.0), 1.0)
        self.assertEqual(erlang_c(agents=5, traffic_intensity=5.0), 1.0)

    def test_valid_probability_range(self):
        p = erlang_c(agents=10, traffic_intensity=5.0)
        self.assertGreaterEqual(p, 0.0)
        self.assertLessEqual(p, 1.0)

    def test_more_agents_lower_probability(self):
        # More agents → lower probability of waiting
        p_few = erlang_c(agents=6, traffic_intensity=5.0)
        p_many = erlang_c(agents=20, traffic_intensity=5.0)
        self.assertGreater(p_few, p_many)


class ServiceLevelTests(TestCase):
    def test_overloaded_returns_zero(self):
        # Insufficient staffing → 0% SL
        result = service_level(agents=2, calls_per_hour=100, avg_handle_time=300, target_answer_time=20)
        self.assertEqual(result, 0.0)

    def test_high_staffing_near_100pct(self):
        result = service_level(agents=50, calls_per_hour=10, avg_handle_time=180, target_answer_time=20)
        self.assertGreater(result, 95.0)

    def test_result_capped_at_100(self):
        result = service_level(agents=100, calls_per_hour=5, avg_handle_time=60, target_answer_time=20)
        self.assertLessEqual(result, 100.0)
        self.assertGreaterEqual(result, 0.0)

    def test_more_agents_higher_sl(self):
        sl_low = service_level(8, 60, 300, 20)
        sl_high = service_level(20, 60, 300, 20)
        self.assertGreater(sl_high, sl_low)


class AgentsRequiredTests(TestCase):
    def test_returns_integer(self):
        n = agents_required(60, 300, 80.0, 20)
        self.assertIsInstance(n, int)

    def test_zero_calls_returns_one(self):
        self.assertEqual(agents_required(0, 300, 80.0, 20), 1)
        self.assertEqual(agents_required(60, 0, 80.0, 20), 1)

    def test_more_calls_requires_more_agents(self):
        low = agents_required(30, 300, 80.0, 20)
        high = agents_required(120, 300, 80.0, 20)
        self.assertLess(low, high)

    def test_higher_target_sl_requires_more_agents(self):
        n_80 = agents_required(60, 300, 80.0, 20)
        n_95 = agents_required(60, 300, 95.0, 20)
        self.assertLessEqual(n_80, n_95)

    def test_achieved_sl_meets_target(self):
        target = 80.0
        n = agents_required(60, 300, target, 20)
        achieved = service_level(n, 60, 300, 20)
        self.assertGreaterEqual(achieved, target)


class ParseAHTTests(TestCase):
    def test_hhmmss(self):
        self.assertEqual(parse_aht('0:07:30'), 450)

    def test_hhmmss_with_hours(self):
        self.assertEqual(parse_aht('1:00:00'), 3600)

    def test_hhmm_two_part(self):
        # Two-part strings are treated as H:M (not M:S)
        self.assertEqual(parse_aht('0:07'), 420)  # 7 minutes

    def test_strips_milliseconds(self):
        self.assertEqual(parse_aht('0:07:30.500'), 450)

    def test_empty_string_returns_zero(self):
        self.assertEqual(parse_aht(''), 0)

    def test_none_returns_zero(self):
        self.assertEqual(parse_aht(None), 0)

    def test_invalid_returns_zero(self):
        self.assertEqual(parse_aht('not-a-time'), 0)


class CalculateStaffingTests(TestCase):
    def _rows(self):
        return [{'day': 'Mon', 'hour': 9, 'avg_calls': 60}]

    def test_shrinkage_increases_headcount(self):
        base = calculate_staffing(self._rows(), 80.0, 20, 0, 300)
        with_shrink = calculate_staffing(self._rows(), 80.0, 20, 20, 300)
        self.assertGreaterEqual(with_shrink[0]['agents_shrinkage'], base[0]['agents_shrinkage'])

    def test_zero_calls_gives_one_agent(self):
        rows = [{'day': 'Mon', 'hour': 9, 'avg_calls': 0}]
        result = calculate_staffing(rows, 80.0, 20, 0, 300)
        self.assertEqual(result[0]['agents_required'], 1)

    def test_output_contains_required_keys(self):
        result = calculate_staffing(self._rows(), 80.0, 20, 0, 300)
        row = result[0]
        for key in ('agents_required', 'agents_shrinkage', 'service_level_achieved', 'hour_label'):
            self.assertIn(key, row)

    def test_passthrough_preserves_input_fields(self):
        rows = [{'day': 'Wednesday', 'hour': 14, 'avg_calls': 45, 'custom': 'x'}]
        result = calculate_staffing(rows, 80.0, 20, 0, 300)
        self.assertEqual(result[0]['day'], 'Wednesday')
        self.assertEqual(result[0]['custom'], 'x')
