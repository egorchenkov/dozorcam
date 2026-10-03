#!/usr/bin/env python3
"""Контракт ONVIF motion gate: парсинг PullMessages и правило hold/fail-open."""
from __future__ import annotations

import pathlib
import sys
import time
import unittest

from cctv.engine.onvif_motion_gate import OnvifMotionGate, has_active_motion  # noqa: E402

MOTION_TRUE = """<s:Envelope><s:Body><wsnt:PullMessagesResponse>
<wsnt:NotificationMessage>
<wsnt:Topic>tns1:RuleEngine/CellMotionDetector/Motion</wsnt:Topic>
<wsnt:Message><tt:Message><tt:Data><tt:SimpleItem Name="IsMotion" Value="true"/></tt:Data></tt:Message></wsnt:Message>
</wsnt:NotificationMessage>
</wsnt:PullMessagesResponse></s:Body></s:Envelope>"""

MOTION_FALSE = """<s:Envelope><s:Body><wsnt:PullMessagesResponse>
<wsnt:NotificationMessage>
<wsnt:Topic>tns1:RuleEngine/CellMotionDetector/Motion</wsnt:Topic>
<wsnt:Message><tt:Message><tt:Data><tt:SimpleItem Name="IsMotion" Value="false"/></tt:Data></tt:Message></wsnt:Message>
</wsnt:NotificationMessage>
</wsnt:PullMessagesResponse></s:Body></s:Envelope>"""

# Живой формат городской камеры 04.09.2026: Tamper=true соседствует с Motion=false
# в одном ответе — глобальный поиск по всему тексту принял бы это за движение.
MOTION_FALSE_TAMPER_TRUE = """<s:Envelope><s:Body><wsnt:PullMessagesResponse>
<wsnt:NotificationMessage>
<wsnt:Topic>tns1:RuleEngine/CellMotionDetector/Motion</wsnt:Topic>
<wsnt:Message><tt:Message><tt:Data><tt:SimpleItem Name="IsMotion" Value="false"/></tt:Data></tt:Message></wsnt:Message>
</wsnt:NotificationMessage>
<wsnt:NotificationMessage>
<wsnt:Topic>tns1:RuleEngine/TamperDetector/Tamper</wsnt:Topic>
<wsnt:Message><tt:Message><tt:Data><tt:SimpleItem Name="IsTamper" Value="true"/></tt:Data></tt:Message></wsnt:Message>
</wsnt:NotificationMessage>
</wsnt:PullMessagesResponse></s:Body></s:Envelope>"""

NO_EVENTS = """<s:Envelope><s:Body><wsnt:PullMessagesResponse/></s:Body></s:Envelope>"""

FIELD_DETECTOR_TRUE = """<s:Envelope><s:Body><wsnt:PullMessagesResponse>
<wsnt:NotificationMessage>
<wsnt:Topic>tns1:RuleEngine/FieldDetector/ObjectsInside</wsnt:Topic>
<wsnt:Message><tt:Message><tt:Data><tt:SimpleItem Name="IsInside" Value="true"/></tt:Data></tt:Message></wsnt:Message>
</wsnt:NotificationMessage>
</wsnt:PullMessagesResponse></s:Body></s:Envelope>"""


class HasActiveMotionTest(unittest.TestCase):
    def test_true_motion_topic_is_detected(self):
        self.assertTrue(has_active_motion(MOTION_TRUE))

    def test_false_motion_topic_is_not_active(self):
        self.assertFalse(has_active_motion(MOTION_FALSE))

    def test_unrelated_true_topic_does_not_leak_into_motion(self):
        # Регрессия: Tamper=true в соседнем сообщении не должно открывать гейт.
        self.assertFalse(has_active_motion(MOTION_FALSE_TAMPER_TRUE))

    def test_empty_pull_is_not_active(self):
        self.assertFalse(has_active_motion(NO_EVENTS))

    def test_field_detector_counts_as_motion(self):
        self.assertTrue(has_active_motion(FIELD_DETECTOR_TRUE))


class GateStateTest(unittest.TestCase):
    def gate(self, hold_seconds=20.0):
        return OnvifMotionGate("dacha", "http://example/onvif/Events", "u", "p", hold_seconds=hold_seconds)

    def test_unhealthy_subscription_fails_open(self):
        # Пока подписка не поднялась ни разу, гейт не должен душить YOLO —
        # сломанный второй протокол не обязан ослеплять уже рабочий детектор.
        gate = self.gate()
        self.assertTrue(gate.is_open())

    def test_healthy_gate_is_closed_without_recent_motion(self):
        gate = self.gate()
        with gate._lock:
            gate._healthy = True
        self.assertFalse(gate.is_open())

    def test_gate_opens_right_after_motion_and_holds(self):
        gate = self.gate(hold_seconds=20.0)
        with gate._lock:
            gate._healthy = True
            gate._last_motion_at = time.time()
        self.assertTrue(gate.is_open())

    def test_gate_closes_after_hold_expires(self):
        gate = self.gate(hold_seconds=5.0)
        with gate._lock:
            gate._healthy = True
            gate._last_motion_at = time.time() - 6.0
        self.assertFalse(gate.is_open())


class ActiveBetweenTest(unittest.TestCase):
    """Кадр из буфера снят в прошлом: гейт обязан отвечать про ЕГО время."""

    def gate(self):
        gate = OnvifMotionGate("city", "http://example/onvif/Events", "u", "p")
        with gate._lock:
            gate._healthy = True
        return gate

    def test_motion_inside_frame_window_opens_gate(self):
        gate = self.gate()
        gate.note_motion(1000.0)
        self.assertTrue(gate.active_between(990.0, 1015.0))

    def test_motion_outside_frame_window_keeps_gate_closed(self):
        gate = self.gate()
        gate.note_motion(1000.0)
        self.assertFalse(gate.active_between(1100.0, 1125.0))

    def test_old_motion_does_not_reopen_a_later_frame(self):
        # Регрессия замысла: движение час назад не повод смотреть кадр сейчас.
        gate = self.gate()
        gate.note_motion(time.time() - 3600)
        now = time.time()
        self.assertFalse(gate.active_between(now - 10, now + 15))

    def test_stale_frame_still_matches_its_own_motion(self):
        # Главный смысл active_between: событие 40 с назад и кадр 40 с назад
        # совпадают, хотя «сейчас» гейт давно закрыт.
        gate = self.gate()
        moment = time.time() - 40
        gate.note_motion(moment)
        self.assertFalse(gate.is_open())
        self.assertTrue(gate.active_between(moment - 10, moment + 15))

    def test_unhealthy_gate_fails_open_for_frames_too(self):
        gate = OnvifMotionGate("city", "http://example/onvif/Events", "u", "p")
        self.assertTrue(gate.active_between(0.0, 1.0))

    def test_motion_log_is_pruned_and_counted(self):
        gate = self.gate()
        gate.remember_seconds = 60.0
        gate.note_motion(time.time() - 600)
        gate.note_motion(time.time())
        self.assertEqual(2, gate.motion_count)
        self.assertEqual(1, len(gate._motion_times))


if __name__ == "__main__":
    unittest.main(verbosity=2)
