#!/usr/bin/env python3
"""Как гейт подключается к камере: адрес ONVIF и время кадра из буфера."""
from __future__ import annotations

import datetime
import pathlib
import sys
import unittest
from unittest import mock

from cctv.engine.cctv_bridge import Camera  # noqa: E402
from cctv.engine import cctv_pipeline  # noqa: E402


def camera(**overrides) -> Camera:
    fields = dict(camera_id="city", title="Город", site="Город",
                  rtsp_url="rtsp://admin:pw@192.0.2.10:554/Streaming/Channels/101",
                  snapshot_url="http://192.0.2.10/onvif-http/snapshot?Profile_1",
                  snapshot_user="admin", snapshot_password="pw", person_detection=True)
    fields.update(overrides)
    return Camera(**fields)


class OnvifHostTest(unittest.TestCase):
    def test_proxied_rtsp_does_not_become_the_onvif_host(self):
        """Регрессия 04.09.2026: в рантайме rtsp_url — это loopback
        credential-прокси, и гейт стучался в 127.0.0.1:80, получая
        Connection refused каждые 10 с. Настоящий адрес даёт snapshot_url."""
        proxied = camera(rtsp_url="rtsp://127.0.0.1:18554/city")
        self.assertEqual("192.0.2.10", cctv_pipeline.onvif_host(proxied))
        with mock.patch.object(cctv_pipeline, "MOTION_GATE_ENABLED", True):
            gate = cctv_pipeline.build_motion_gate(proxied)
        self.assertEqual("http://192.0.2.10/onvif/Events", gate.events_url)

    def test_onvif_gate_is_off_by_default(self):
        """Замер 04.09.2026: VMD дачи не сработал на смену 97.7 % кадра,
        поэтому ONVIF-гейт по умолчанию выключен и закрывать гейт не может."""
        self.assertFalse(cctv_pipeline.MOTION_GATE_ENABLED)
        self.assertIsNone(cctv_pipeline.build_motion_gate(camera()))

    def test_direct_config_still_works(self):
        self.assertEqual("192.0.2.10", cctv_pipeline.onvif_host(camera()))

    def test_dacha_snapshot_url_with_port_gives_bare_host(self):
        dacha = camera(camera_id="dacha", rtsp_url="rtsp://127.0.0.1:18556/dacha",
                        snapshot_url="http://198.51.100.201:80/onvif/Snapshot",
                        snapshot_user="admin", snapshot_password="admin")
        self.assertEqual("198.51.100.201", cctv_pipeline.onvif_host(dacha))

    def test_camera_without_credentials_gets_no_gate(self):
        with mock.patch.object(cctv_pipeline, "MOTION_GATE_ENABLED", True):
            self.assertIsNone(cctv_pipeline.build_motion_gate(
                camera(snapshot_user=None, snapshot_password=None)))

    def test_all_loopback_addresses_give_no_host(self):
        blind = camera(rtsp_url="rtsp://127.0.0.1:18554/x", snapshot_url="http://localhost/snap")
        self.assertIsNone(cctv_pipeline.onvif_host(blind))


class SegmentTimestampTest(unittest.TestCase):
    def test_segment_name_is_read_as_utc(self):
        moment = cctv_pipeline.segment_started_at(pathlib.Path("2026-09-04T08:41:03Z.ts"))
        expected = datetime.datetime(2026, 9, 4, 8, 41, 3, tzinfo=datetime.timezone.utc).timestamp()
        self.assertEqual(expected, moment)

    def test_foreign_name_is_not_an_exception(self):
        self.assertIsNone(cctv_pipeline.segment_started_at(pathlib.Path("clip.ts")))
        self.assertIsNone(cctv_pipeline.segment_started_at(None))


class GateThresholdTest(unittest.TestCase):
    """Порог гейта на камеру: замер 21.09.2026 показал, что общий 0.15 % на
    ночной сцене dacha3 (frame-diff p50 1.1 %) не закрывается никогда —
    saved=0 %, CPU конвейера 340 %."""

    def test_quiet_scene_keeps_the_base(self):
        quiet = [0.018] * cctv_pipeline.PERSON_GATE_NOISE_WINDOW
        self.assertEqual(cctv_pipeline.PERSON_GATE_THRESHOLD,
                         cctv_pipeline.gate_threshold_for(cctv_pipeline.PERSON_GATE_THRESHOLD, quiet))

    def test_noisy_night_scene_raises_the_floor(self):
        night = [1.1] * cctv_pipeline.PERSON_GATE_NOISE_WINDOW
        threshold = cctv_pipeline.gate_threshold_for(cctv_pipeline.PERSON_GATE_THRESHOLD, night)
        self.assertAlmostEqual(1.1 * cctv_pipeline.PERSON_GATE_NOISE_FACTOR, threshold)

    def test_floor_never_exceeds_the_cap(self):
        broken = [40.0] * cctv_pipeline.PERSON_GATE_NOISE_WINDOW
        self.assertEqual(cctv_pipeline.PERSON_GATE_NOISE_MAX,
                         cctv_pipeline.gate_threshold_for(0.15, broken))

    def test_floor_never_lowers_the_camera_base(self):
        quiet = [0.0] * cctv_pipeline.PERSON_GATE_NOISE_WINDOW
        self.assertEqual(0.8, cctv_pipeline.gate_threshold_for(0.8, quiet))

    def test_floor_uses_upper_quartile_not_median(self):
        """Замер 25.09.2026: медиана пропускала 40–70 % кадров шумной сцены,
        потому что половина замеров по построению выше неё."""
        window = [0.1] * 300 + [1.0] * 300  # p50 = 0.1, p75 = 1.0
        threshold = cctv_pipeline.gate_threshold_for(0.15, window)
        self.assertAlmostEqual(1.0 * cctv_pipeline.PERSON_GATE_NOISE_FACTOR, threshold)

    def test_short_window_is_not_a_measurement(self):
        """После рестарта первые кадры не должны задирать порог."""
        self.assertEqual(0.15, cctv_pipeline.gate_threshold_for(0.15, [5.0, 5.0, 5.0]))

    def test_person_in_frame_does_not_move_the_floor(self):
        """Долгий человек в кадре не должен поднимать пол: его кадры — не шум покоя."""
        import collections
        noise = collections.deque(maxlen=cctv_pipeline.PERSON_GATE_NOISE_WINDOW)
        for _ in range(cctv_pipeline.PERSON_GATE_NOISE_WINDOW * 2 // 3):
            cctv_pipeline.remember_noise(noise, 0.02, False)
        for _ in range(cctv_pipeline.PERSON_GATE_NOISE_WINDOW // 3):
            cctv_pipeline.remember_noise(noise, 6.0, True)
        self.assertEqual(0.15, cctv_pipeline.gate_threshold_for(0.15, noise))

    def test_wind_that_passed_the_gate_is_still_noise(self):
        import collections
        noise = collections.deque()
        cctv_pipeline.remember_noise(noise, 1.5, False)
        cctv_pipeline.remember_noise(noise, None, False)
        self.assertEqual([1.5], list(noise))

    def test_camera_base_comes_from_the_registry(self):
        self.assertEqual(2.0, camera(person_gate_threshold=2.0).person_gate_threshold)
        self.assertIsNone(camera().person_gate_threshold)


if __name__ == "__main__":
    unittest.main(verbosity=2)
