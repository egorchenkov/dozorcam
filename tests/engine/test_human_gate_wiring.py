#!/usr/bin/env python3
"""Гейт YOLO по людям от самой камеры (Hikvision FieldDetection через ONVIF)."""
from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

from cctv.engine.cctv_bridge import Camera  # noqa: E402
from cctv.engine import cctv_pipeline  # noqa: E402
from cctv.engine.onvif_motion_gate import has_active_motion  # noqa: E402

MOTION_TRUE = """<s:Envelope><s:Body><wsnt:PullMessagesResponse>
<wsnt:NotificationMessage>
<wsnt:Topic>tns1:VideoSource/MotionAlarm</wsnt:Topic>
<wsnt:Message><tt:Message><tt:Data><tt:SimpleItem Name="IsMotion" Value="true"/></tt:Data></tt:Message></wsnt:Message>
</wsnt:NotificationMessage>
</wsnt:PullMessagesResponse></s:Body></s:Envelope>"""

FIELD_TRUE = """<s:Envelope><s:Body><wsnt:PullMessagesResponse>
<wsnt:NotificationMessage>
<wsnt:Topic>tns1:RuleEngine/FieldDetector/ObjectsInside</wsnt:Topic>
<wsnt:Message><tt:Message><tt:Data><tt:SimpleItem Name="IsInside" Value="true"/></tt:Data></tt:Message></wsnt:Message>
</wsnt:NotificationMessage>
</wsnt:PullMessagesResponse></s:Body></s:Envelope>"""


def camera(**overrides) -> Camera:
    fields = dict(camera_id="dacha3", title="Дача-3", site="Дача",
                  rtsp_url="rtsp://127.0.0.1:18560/dacha3",
                  snapshot_url="http://198.51.100.202/onvif-http/snapshot?Profile_1",
                  snapshot_user="admin", snapshot_password="pw", person_detection=True,
                  camera_human_events=True)
    fields.update(overrides)
    return Camera(**fields)


class TopicFilterTest(unittest.TestCase):
    def test_vmd_is_not_a_human_signal(self):
        """dacha3 ночью: VMD с фильтром human срабатывает раз в секунду на куст
        в ИК (23.09.2026), FieldDetection при этом молчит. Гейт по людям обязан
        слушать только FieldDetector."""
        self.assertFalse(has_active_motion(MOTION_TRUE, ("FieldDetector",)))
        self.assertTrue(has_active_motion(FIELD_TRUE, ("FieldDetector",)))

    def test_default_topics_still_count_vmd(self):
        self.assertTrue(has_active_motion(MOTION_TRUE))


class HumanGateWiringTest(unittest.TestCase):
    def test_flagged_camera_gets_field_detector_subscription(self):
        gate = cctv_pipeline.build_human_gate(camera())
        self.assertEqual("http://198.51.100.202/onvif/Events", gate.events_url)
        self.assertEqual(("FieldDetector",), gate.topics)
        self.assertEqual("human_gate", gate.label)
        self.assertEqual(cctv_pipeline.HUMAN_GATE_HOLD_SECONDS, gate.hold_seconds)

    def test_default_mode_is_shadow(self):
        """Решение владельца 26.09.2026: сначала тень, enforce — после набора
        подтверждённых проходов."""
        self.assertEqual("shadow", cctv_pipeline.HUMAN_GATE_MODE)

    def test_camera_without_flag_gets_no_gate(self):
        """Город (G0) и Tantos цель не классифицируют: их frame-diff
        гейт не трогаем."""
        self.assertIsNone(cctv_pipeline.build_human_gate(camera(camera_human_events=False)))

    def test_mode_off_disables_everything(self):
        with mock.patch.object(cctv_pipeline, "HUMAN_GATE_MODE", "off"):
            self.assertIsNone(cctv_pipeline.build_human_gate(camera()))

    def test_no_credentials_no_gate(self):
        self.assertIsNone(cctv_pipeline.build_human_gate(camera(snapshot_user=None, snapshot_password=None)))

    def test_registry_flag_is_strict_boolean(self):
        from cctv.engine.cctv_bridge import Bridge
        raw = dict(camera_id="x", title="x", rtsp_url="rtsp://h/x", camera_human_events="true")
        self.assertFalse(Bridge._cameras({"cameras": [raw]})[0].camera_human_events)
        raw["camera_human_events"] = True
        self.assertTrue(Bridge._cameras({"cameras": [raw]})[0].camera_human_events)


class EventLabelTest(unittest.TestCase):
    def test_camera_human_label_comes_from_camera_not_from_overrides(self):
        """26.09.2026: keepalive и forced_scan переоткрывают human_closed, и метка
        camera_human, взятая из него, стала «1» у шести ложных YOLO-тревог
        dacha3 (мешки в углу, 0.35–0.41) при events=0 у камеры за весь день.
        Цикл detect() целиком не прогнать без камеры, поэтому проверяем
        источник метки по исходнику: только camera_saw, не human_closed."""
        import inspect
        source = inspect.getsource(cctv_pipeline.detect)
        self.assertIn("camera_human={int(bool(camera_saw))}", source)
        self.assertNotIn("camera_human={int(not human_closed)}", source)
        # Счётчики confirmed/unconfirmed — из того же источника.
        body = source.split("seen = f\" camera_human", 1)[1]
        self.assertIn("if not camera_saw:", body.split("human_confirmed += 1", 1)[0])


if __name__ == "__main__":
    unittest.main()
