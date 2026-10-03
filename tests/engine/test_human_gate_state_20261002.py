import pathlib, sys, unittest

from cctv.engine.onvif_motion_gate import OnvifMotionGate, motion_states


def msg(value):
    return ('<tt:NotificationMessage><x>RuleEngine/FieldDetector/ObjectsInside</x>'
            f'<Item Name="IsInside" Value="{value}"/></tt:NotificationMessage>')


class StateTelemetry(unittest.TestCase):
    def gate(self):
        g = OnvifMotionGate("c", "http://x", "u", "p", topics=("FieldDetector",))
        g._healthy = True
        return g

    def test_states_parsed_in_order(self):
        self.assertEqual(motion_states(msg("true") + msg("false"), ("FieldDetector",)), [True, False])

    def test_interval_covers_frame_after_trigger_window(self):
        g = self.gate()
        g.note_state(True, 1000); g.note_state(False, 1100)
        self.assertTrue(g.state_active_near(1090))   # далеко за окном +45 с от триггера
        self.assertTrue(g.state_active_near(1150))   # запас 60 с
        self.assertFalse(g.state_active_near(1200))

    def test_open_interval_runs_to_now(self):
        g = self.gate()
        g.note_state(True, 2000)
        self.assertTrue(g.state_active_near(2500))
        self.assertFalse(g.state_active_near(1000))


if __name__ == "__main__":
    unittest.main()
