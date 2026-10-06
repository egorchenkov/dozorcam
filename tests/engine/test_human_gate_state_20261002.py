import pathlib, sys, unittest

from cctv.engine.onvif_motion_gate import OnvifMotionGate, motion_states, state_messages


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

    def test_open_interval_capped_after_last_true(self):
        # 03–04.10.2026: inactive по ONVIF потерялся — интервал тянулся 30+ ч,
        # state_seen прода был 100 %. Без подтверждения цель живёт state_cap_seconds.
        g = self.gate()
        g.note_state(True, 2000)
        self.assertTrue(g.state_active_near(2300))
        self.assertFalse(g.state_active_near(2500))
        self.assertFalse(g.state_active_near(1000))

    def test_repeated_true_extends_interval(self):
        g = self.gate()
        g.note_state(True, 2000); g.note_state(True, 2250)
        self.assertTrue(g.state_active_near(2500))

    def test_new_true_after_cap_starts_new_interval(self):
        g = self.gate()
        g.note_state(True, 2000); g.note_state(True, 5000)
        self.assertFalse(g.state_active_near(3500))
        self.assertTrue(g.state_active_near(5010))

    def test_inactive_of_one_target_keeps_other(self):
        g = self.gate()
        g.note_state(True, 1000, key="R/1"); g.note_state(True, 1000, key="R/2")
        g.note_state(False, 1030, key="R/1")
        self.assertTrue(g.state_active_near(1150))   # R/2 ещё в зоне
        g.note_state(False, 1200, key="R/2")
        self.assertFalse(g.state_active_near(1300))

    def test_resubscribe_forgets_targets_without_inactive(self):
        g = self.gate()
        g.note_state(True, 1000, key="R/1")
        g.reset_state(1050)
        g.note_state(False, 1051, key="R/0")         # Initialized false новой подписки
        self.assertFalse(g.state_active_near(1200))

    def test_hikvision_initialized_message(self):
        raw = ('<wsnt:NotificationMessage><wsnt:Topic>tns1:RuleEngine/FieldDetector/ObjectsInside</wsnt:Topic>'
               '<tt:Message UtcTime="2026-10-04T19:50:54Z" PropertyOperation="Initialized"><tt:Source>'
               '<tt:SimpleItem Name="VideoSourceConfigurationToken" Value="VideoSourceToken"/>'
               '<tt:SimpleItem Name="VideoAnalyticsConfigurationToken" Value="VideoAnalyticsToken"/>'
               '<tt:SimpleItem Name="Rule" Value="MyFieldDetector1"/></tt:Source>'
               '<tt:Key><tt:SimpleItem Name="ObjectId" Value="0"/></tt:Key>'
               '<tt:Data><tt:SimpleItem Name="IsInside" Value="false"/></tt:Data></tt:Message></wsnt:NotificationMessage>')
        self.assertEqual(state_messages(raw, ("FieldDetector",)),
                         [("VideoSourceToken/MyFieldDetector1/0", False, "Initialized")])


if __name__ == "__main__":
    unittest.main()
