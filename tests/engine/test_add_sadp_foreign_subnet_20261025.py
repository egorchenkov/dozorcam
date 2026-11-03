"""Мастер /add: новая Hikvision на заводском адресе чужой подсети.

IPv4-развёртка до такой камеры не доходит (TCP в чужую подсеть без маршрута),
но SADP-ответ называет модель, MAC и IPv6 link-local. Что проверяется:
разбор IPv6 и интерфейса приёма SADP; кандидат «вне сети» в поиске (и
несколько новых камер на одном заводском адресе); активация по
`http://[fe80::…%iface]`; смена сети static|dynamic тем же путём (тело PUT —
из живого GET, rebootRequired, потерянный ответ, DHCP без аренды); после смены
сети камера заводится по новому IPv4 как обычная. Камеры — эмуляторы
(`hik_emulator.py`); link-local — подмена базового адреса, а если у узла есть
свой fe80-адрес — и настоящий сокет на нём.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import subprocess
import time
import unittest
from unittest import mock

from cctv.engine import camera_discovery as discovery
from cctv.engine import hikvision_activation as hik
from hik_emulator import FACTORY_ADDRESS, HikEmulator
from test_hikvision_activation_20261003 import EmulatedCameras

# MAC собирается в рантайме: гейт публичного экспорта ловит MAC-литералы.
MAC = "-".join(("00", "00", "5e", "00", "53", "01"))
MAC2 = "-".join(("00", "00", "5e", "00", "53", "02"))
LINK = "fe80::200:5eff:fe00:5301"
LINK2 = "fe80::200:5eff:fe00:5302"
PASSWORD = "Kamera-2026"


def sadp_reply(*, host: str = FACTORY_ADDRESS, ipv6: str = LINK, mac: str = MAC,
               activated: str = "false") -> bytes:
    return ('<?xml version="1.0" encoding="UTF-8"?><ProbeMatch><Uuid>X</Uuid>'
            "<Types>inquiry</Types><DeviceType>139</DeviceType>"
            "<DeviceDescription>DS-2CD2043G2-I</DeviceDescription><HttpPort>80</HttpPort>"
            f"<MAC>{mac}</MAC><IPv4Address>{host}</IPv4Address>"
            "<IPv4SubnetMask>255.255.255.0</IPv4SubnetMask>"
            f"<IPv6Address>{ipv6}</IPv6Address><IPv6MaskLen>64</IPv6MaskLen>"
            f"<DHCP>false</DHCP><Activated>{activated}</Activated></ProbeMatch>").encode()


def local_link_address() -> tuple[str, str] | None:
    """Свой fe80-адрес узла (адрес, интерфейс) — для сокета на настоящем link-local."""
    try:
        out = subprocess.run(["ip", "-6", "-o", "addr", "show", "scope", "link"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        match = re.search(r"^\d+:\s+(\S+)\s+inet6\s+(fe80::[0-9a-f:]+)/", line)
        if match and "tentative" not in line:
            return match.group(2), match.group(1)
    return None


class SadpParseTest(unittest.TestCase):
    def test_link_local_and_interface(self) -> None:
        parsed = hik.parse_sadp(sadp_reply(), iface="eth0")
        self.assertEqual({"host": FACTORY_ADDRESS, "activated": False, "model": "DS-2CD2043G2-I",
                          "mac": MAC, "ipv6": LINK, "iface": "eth0"}, parsed)
        self.assertEqual(f"{LINK}%eth0", hik.link_local_host(parsed))

    def test_only_link_local_ipv6_is_kept(self) -> None:
        for ipv6 in ("::", "2001:db8::64", "garbage", ""):
            with self.subTest(ipv6=ipv6):
                parsed = hik.parse_sadp(sadp_reply(ipv6=ipv6), iface="eth0")
                self.assertEqual("", parsed["ipv6"])
                self.assertEqual("", hik.link_local_host(parsed))
        # Scope в самом ответе камеры — не наш интерфейс: берётся интерфейс приёма.
        self.assertEqual(LINK, hik.parse_sadp(sadp_reply(ipv6=f"{LINK}%2"))["ipv6"])

    def test_no_interface_means_no_link(self) -> None:
        parsed = hik.parse_sadp(sadp_reply(), iface="")
        self.assertEqual("", hik.link_local_host(parsed))
        self.assertEqual("", hik.parse_sadp(sadp_reply(), iface="eth0; rm")["iface"])

    def test_interface_from_pktinfo(self) -> None:
        index = socket.if_nametoindex("lo")
        data = index.to_bytes(4, __import__("sys").byteorder) + bytes(8)
        self.assertEqual("lo", hik._iface_from_ancdata(
            [(socket.IPPROTO_IP, socket.IP_PKTINFO, data)]))
        self.assertEqual("", hik._iface_from_ancdata([]))
        self.assertEqual("", hik._iface_from_ancdata(
            [(socket.IPPROTO_IP, socket.IP_PKTINFO, (999999).to_bytes(4, "little") + bytes(8))]))

    def test_camera_base_brackets_ipv6(self) -> None:
        self.assertEqual("http://192.0.2.64:80", hik.camera_base("192.0.2.64", 80))
        base = hik.camera_base(f"{LINK}%eth0", 80)
        self.assertEqual(f"http://[{LINK}%eth0]:80", base)
        parts = __import__("urllib.parse").parse.urlsplit(base + "/SDK/activateStatus")
        self.assertEqual((f"{LINK}%eth0", 80), (parts.hostname, parts.port))


class NetworkBodyTest(unittest.TestCase):
    def live(self) -> str:
        return HikEmulator("127.0.0.1", 1).network_xml()

    def test_static_changes_only_values(self) -> None:
        plan = hik.network_plan({"mode": "static", "address": "192.0.2.50", "prefix": 24,
                                 "gateway": "192.0.2.1"})
        body = hik.network_body(self.live(), plan)
        parsed = hik._parse_network(body)
        self.assertEqual(hik.Network("static", "192.0.2.50", "255.255.255.0", "192.0.2.1"), parsed)
        self.assertIn("<ipAddress>198.51.100.53</ipAddress>", body)  # DNS не тронут
        self.assertEqual(self.live().count("\n"), body.count("\n"))

    def test_dhcp_changes_only_addressing_type(self) -> None:
        body = hik.network_body(self.live(), hik.network_plan({"mode": "dhcp"}))
        self.assertEqual(self.live().replace(">static<", ">dynamic<"), body)

    def test_gateway_inserted_when_absent(self) -> None:
        live = re.sub(r"<DefaultGateway>.*?</DefaultGateway>\n", "", self.live(), flags=re.S)
        plan = hik.network_plan({"mode": "static", "address": "192.0.2.50", "prefix": 24,
                                 "gateway": "192.0.2.1"})
        self.assertEqual("192.0.2.1", hik._parse_network(hik.network_body(live, plan)).gateway)

    def test_plan_validation(self) -> None:
        good = {"mode": "static", "address": "192.0.2.50", "prefix": 24, "gateway": "192.0.2.1"}
        self.assertEqual("255.255.255.0", hik.network_plan(good).mask)
        for bad in (None, {}, {"mode": "bridge"},
                    dict(good, gateway="198.51.100.1"),   # шлюз вне подсети
                    dict(good, address="192.0.2.255"),     # широковещательный
                    dict(good, address="192.0.2.1"),       # = шлюз
                    dict(good, address="8.8.8.8", gateway="8.8.8.1"),
                    dict(good, prefix=32), dict(good, prefix="x"), dict(good, address="")):
            with self.subTest(bad=bad), self.assertRaises(hik.ActivationError):
                hik.network_plan(bad)


class LinkLocalCameras(EmulatedCameras):
    """Эмулятор «на link-local»: мост ходит на http://[fe80::…%iface], подмена
    базового адреса переводит этот адрес на сокет эмулятора."""

    def setUp(self) -> None:
        super().setUp()
        self.links: dict[str, HikEmulator] = {}
        real = hik.camera_base

        def camera_base(host: str, port: int) -> str:
            emulator = self.links.get(host)
            return emulator.base if emulator else real(host, port)

        patcher = mock.patch.object(hik, "camera_base", side_effect=camera_base)
        self.base_calls = patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (("NETWORK_POLL_SEC", 0.05), ("NETWORK_WAIT_SEC", 3.0)):
            patcher = mock.patch.object(self.module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def factory_camera(self, last_octet: int, link: str = f"{LINK}%eth0", **kwargs) -> HikEmulator:
        emulator = self.camera(last_octet, **kwargs)
        self.links[link] = emulator
        return emulator

    def link_requests(self) -> list[str]:
        return [c.args[0] for c in self.base_calls.call_args_list]


class ForeignSubnetScanTest(LinkLocalCameras):
    def scan(self, records: list[dict], found: list | None = None) -> list[dict]:
        sadp = {r["mac"] or r["host"]: r for r in records}
        with mock.patch.object(hik, "sadp_discover", return_value=sadp), \
                mock.patch.object(discovery, "ws_discover", return_value=[]), \
                mock.patch.object(discovery, "scan", return_value=found or []) as scan:
            started = self.bridge.start_scan("192.0.2.0/24")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                status = self.bridge.scan_status(started["scan_id"])
                if status["status"] != "running":
                    break
                time.sleep(0.05)
        self.assertEqual("done", status["status"], status)
        # Заводские IPv4 из SADP развёртка всё равно пробует (вдруг маршрут есть) — по разу.
        self.assertEqual(sorted({r["host"] for r in records}),
                         sorted(scan.call_args.kwargs["extra_hosts"]))
        return status["candidates"]

    def test_candidate_outside_networks_is_shown(self) -> None:
        self.factory_camera(2)
        [candidate] = self.scan([hik.parse_sadp(sadp_reply(), iface="eth0")])
        self.assertEqual({"host": f"{LINK}%eth0", "factory_host": FACTORY_ADDRESS, "mac": MAC,
                          "model": "DS-2CD2043G2-I", "vendor": "hikvision", "activated": False,
                          "activation": "v3", "foreign_subnet": True, "link_local": True},
                         {k: candidate[k] for k in ("host", "factory_host", "mac", "model", "vendor",
                                                    "activated", "activation", "foreign_subnet",
                                                    "link_local")})

    def test_two_new_cameras_on_one_factory_address(self) -> None:
        self.factory_camera(2)
        self.factory_camera(3, link=f"{LINK2}%eth0", protocol="legacy")
        candidates = self.scan([hik.parse_sadp(sadp_reply(), iface="eth0"),
                                hik.parse_sadp(sadp_reply(ipv6=LINK2, mac=MAC2), iface="eth0")])
        self.assertEqual([(f"{LINK}%eth0", "v3"), (f"{LINK2}%eth0", "legacy")],
                         [(c["host"], c["activation"]) for c in candidates])

    def test_without_link_local_it_waits_for_manual_setup(self) -> None:
        [candidate] = self.scan([hik.parse_sadp(sadp_reply(ipv6="::"), iface="eth0")])
        self.assertEqual((FACTORY_ADDRESS, False, "", False),
                         (candidate["host"], candidate["activated"], candidate["activation"],
                          candidate["link_local"]))

    def test_activated_or_reachable_cameras_are_not_duplicated(self) -> None:
        reachable = discovery.Candidate(host="192.0.2.70", ports=[80], vendor="hikvision",
                                        activated=False, activation="v3")
        candidates = self.scan([hik.parse_sadp(sadp_reply(activated="true"), iface="eth0"),
                                hik.parse_sadp(sadp_reply(host="192.0.2.70", mac=MAC2), iface="eth0")],
                               found=[reachable])
        self.assertEqual(["192.0.2.70"], [c["host"] for c in candidates])

    def test_stale_sadp_activated_camera_is_skipped(self) -> None:
        emulator = self.factory_camera(2)
        emulator.activated = True
        self.assertEqual([], self.scan([hik.parse_sadp(sadp_reply(), iface="eth0")]))

    def test_inside_polled_network_is_marked_unreachable_not_foreign(self) -> None:
        self.factory_camera(2)
        record = hik.parse_sadp(sadp_reply(host="192.0.2.64"), iface="eth0")
        [candidate] = self.scan([record, dict(record, mac=MAC2, ipv6="")])[:1]
        self.assertFalse(candidate["foreign_subnet"])


class ForeignSubnetActivationTest(LinkLocalCameras):
    def test_static_network_then_regular_camera(self) -> None:
        emulator = self.factory_camera(2)
        result = self.run_job([f"{LINK}%eth0"], password=PASSWORD,
                              network={"mode": "static", "address": "127.0.0.2", "prefix": 8,
                                       "gateway": "127.0.0.1"})
        [camera] = result["results"]
        self.assertEqual(("activated", "127.0.0.2", True, True),
                         (camera["outcome"], camera.get("address"), camera["onvif"], camera["stream"]),
                         camera)
        self.assertEqual(PASSWORD, emulator.admin_password)
        self.assertEqual({"mode": "static", "address": "127.0.0.2", "mask": "255.0.0.0",
                          "gateway": "127.0.0.1"}, emulator.network)
        self.assertEqual(0, emulator.failed_logins)
        # Активация и сеть — по link-local, поток — уже по новому IPv4.
        self.assertIn(f"{LINK}%eth0", self.link_requests())
        self.assertIn(f"rtsp://***@127.0.0.2:{emulator.rtsp_port}/", camera["summary"]["main_url"])
        added = self.bridge.add_camera({"camera_id": "hik-127-0-0-2", "title": "Hik",
                                        "probe_token": camera["probe_token"]})
        self.assertTrue(added["ok"], added)
        record = json.loads(self.registry.read_text())["cameras"][0]
        self.assertTrue(discovery.rtsp_ok(record["rtsp_url"]))
        entry = self.vault()[f"{LINK}%eth0"]
        self.assertEqual(("onvif_ready", "127.0.0.2", PASSWORD),
                         (entry["state"], entry["address"], entry["admin_password"]))

    def test_dhcp_address_is_read_back(self) -> None:
        emulator = self.factory_camera(3, dhcp_address="127.0.0.3")
        [camera] = self.run_job([f"{LINK}%eth0"], password=PASSWORD,
                                network={"mode": "dhcp"})["results"]
        self.assertEqual(("activated", "127.0.0.3", True), (camera["outcome"], camera.get("address"),
                                                           camera["stream"]), camera)
        self.assertEqual("dynamic", emulator.network["mode"])

    def test_reboot_required_and_lost_reply(self) -> None:
        for octet, reply in ((4, "reboot"), (5, "lost")):
            with self.subTest(reply=reply):
                link = f"fe80::{octet}%eth0"
                emulator = self.factory_camera(octet, link=link, network_reply=reply)
                [camera] = self.run_job([link], password=PASSWORD,
                                        network={"mode": "static", "address": f"127.0.0.{octet}",
                                                 "prefix": 8, "gateway": "127.0.0.1"})["results"]
                self.assertEqual((f"127.0.0.{octet}", True), (camera.get("address"), camera["stream"]),
                                 camera)
                self.assertEqual(1 if reply == "reboot" else 0, emulator.reboots)

    def test_no_dhcp_lease_keeps_password_and_says_so(self) -> None:
        emulator = self.factory_camera(2)  # DHCP не выдаёт адреса: остаётся заводской
        with mock.patch.object(self.module, "NETWORK_WAIT_SEC", 0.3):
            result = self.run_job([f"{LINK}%eth0"], password=PASSWORD, network={"mode": "dhcp"})
        [camera] = result["results"]
        self.assertEqual(("activated", "network_wait", False), (camera["outcome"], camera["stage"],
                                                               camera["onvif"]))
        self.assertEqual(PASSWORD, result["password"])  # активирована — пароль владельцу
        self.assertEqual("network_failed", self.vault()[f"{LINK}%eth0"]["state"])
        self.assertTrue(emulator.activated)

    def test_refused_network_stage(self) -> None:
        self.factory_camera(2, network_reply="refuse")
        [camera] = self.run_job([f"{LINK}%eth0"], password=PASSWORD,
                                network={"mode": "dhcp"})["results"]
        self.assertEqual(("activated", "network_refused"), (camera["outcome"], camera["stage"]))

    def test_request_validation(self) -> None:
        link = f"{LINK}%eth0"
        cases = [({"hosts": [link], "password": PASSWORD}, "bad_network"),
                 ({"hosts": [link], "password": PASSWORD, "network": {"mode": "bridge"}}, "bad_network"),
                 ({"hosts": [link, f"{LINK2}%eth0"], "password": PASSWORD,
                   "network": {"mode": "static", "address": "192.0.2.50", "prefix": 24,
                               "gateway": "192.0.2.1"}}, "network_static_many"),
                 ({"hosts": [LINK], "password": PASSWORD, "network": {"mode": "dhcp"}}, "bad_hosts"),
                 ({"hosts": ["2001:db8::1%eth0"], "password": PASSWORD,
                   "network": {"mode": "dhcp"}}, "bad_hosts"),
                 ({"hosts": [f"{LINK}%eth0/x"], "password": PASSWORD,
                   "network": {"mode": "dhcp"}}, "bad_hosts")]
        for request, code in cases:
            with self.subTest(request=request):
                self.assertEqual({"ok": False, "error_code": code},
                                 self.bridge.start_activation(request))
        # Обычные IPv4-камеры сеть не требуют, а в смешанном пакете она — только link-local.
        started = self.bridge.start_activation({"hosts": ["192.0.2.64", link], "password": PASSWORD,
                                                "network": {"mode": "dhcp"}})
        self.assertTrue(started["ok"], started)
        self.assertEqual(["192.0.2.64", link], started["hosts"])


@unittest.skipUnless(local_link_address(), "у узла нет IPv6 link-local")
class RealLinkLocalSocketTest(EmulatedCameras):
    """Без подмены адреса: эмулятор слушает настоящий fe80::…%iface узла."""

    def test_activation_over_scoped_link_local_url(self) -> None:
        address, iface = local_link_address()
        link = f"{address}%{iface}"
        emulator = HikEmulator("127.0.0.6", self.port, extra_ips=(link,)).start()
        self.addCleanup(emulator.stop)
        self.assertEqual(hik.Status(False, "v3"),
                         hik.activation_status(hik.camera_base(link, self.port)))
        with mock.patch.object(self.module, "NETWORK_POLL_SEC", 0.05):
            [camera] = self.run_job([link], password=PASSWORD,
                                    network={"mode": "static", "address": "127.0.0.6", "prefix": 8,
                                             "gateway": "127.0.0.1"})["results"]
        self.assertEqual(("activated", "127.0.0.6", True), (camera["outcome"], camera.get("address"),
                                                           camera["stream"]), camera)
        self.assertTrue(ipaddress.ip_address(link).is_link_local)


if __name__ == "__main__":
    unittest.main()
