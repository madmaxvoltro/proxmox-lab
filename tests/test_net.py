import tomllib
import unittest

from lab import config as config_mod
from lab import doctor, hostio, netutil
from lab import init as init_mod
from lab.db import DB
from lab.errors import PreconditionError, UsageError
from lab.prompt import ScriptedPrompter
from lab.proxmox import Proxmox
from lab.runner import Result
from lab.template import TemplateBuilder

from .fake_io import FakeIO
from .fake_proxmox import FakeProxmox
from .test_m1 import CATALOGUE, StubRunner, make_cfg, make_io
from .test_m2 import Base


def static(cfg, start="192.168.10.200", end="192.168.10.203"):
    n = cfg.network
    n.mode, n.subnet, n.gateway, n.dns = "static", "192.168.10.0/24", "192.168.10.1", "192.168.10.1 1.1.1.1"
    n.pool_start, n.pool_end = start, end
    return cfg.validate()


class NetworkConfigTest(unittest.TestCase):
    def test_dhcp_needs_nothing_and_unknown_mode_fails(self):
        make_cfg().network.validate()
        cfg = make_cfg()
        cfg.network.mode = "magic"
        with self.assertRaises(UsageError):
            cfg.network.validate()

    def test_static_validation(self):
        good = static(make_cfg())
        self.assertEqual(netutil.pool(good.network)[0], "192.168.10.200")
        for field, value in (("gateway", "10.0.0.1"), ("pool_start", "192.168.20.5"), ("pool_end", "192.168.10.100"),
                             ("gateway", "192.168.10.201"), ("dns", ""), ("subnet", "nonsense"),
                             ("pool_end", "192.168.10.255"), ("pool_start", "192.168.10.0")):
            cfg = static(make_cfg())
            setattr(cfg.network, field, value)
            with self.assertRaises(UsageError, msg=f"{field}={value}"):
                cfg.validate()

    def test_roundtrips_through_toml_and_rejects_unknown_keys(self):
        cfg = static(make_cfg())
        again = config_mod.from_dict(tomllib.loads(config_mod.dumps(cfg)))
        self.assertEqual(again.network, cfg.network)
        with self.assertRaises(UsageError):
            config_mod.from_dict({"network": {"vlan_tag": 5}})


class AllocateTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeProxmox()
        self.net = static(make_cfg()).network

    def test_ipconfig(self):
        self.assertEqual(netutil.ipconfig(make_cfg().network), {"ipconfig0": "ip=dhcp"})
        self.assertEqual(netutil.ipconfig(self.net, "192.168.10.200"),
                         {"ipconfig0": "ip=192.168.10.200/24,gw=192.168.10.1", "nameserver": "192.168.10.1 1.1.1.1"})

    def test_picks_first_free_skipping_assigned_and_answering_addresses(self):
        self.assertIsNone(netutil.allocate(make_cfg().network, set(), self.fake))
        self.assertEqual(netutil.allocate(self.net, set(), self.fake), "192.168.10.200")
        self.fake.ips_in_use = {"192.168.10.200"}  # something outside lab already has it
        self.assertEqual(netutil.allocate(self.net, {"192.168.10.201"}, self.fake), "192.168.10.202")

    def test_exhausted_pool(self):
        used = set(netutil.pool(self.net))
        with self.assertRaisesRegex(PreconditionError, "no free address"):
            netutil.allocate(self.net, used, self.fake)

    def test_ping_is_read_only_so_dry_run_still_checks(self):
        from lab.runner import is_read_only
        self.assertTrue(is_read_only(["ping", "-c", "1", "-W", "1", "192.168.10.200"]))
        self.assertTrue(is_read_only(["ip", "-4", "-o", "addr", "show", "dev", "vmbr1"]))

    def test_host_defaults_from_the_bridge(self):
        class R:
            def run(self, argv, **kw):
                if argv[:3] == ["ip", "-4", "-o"]:
                    return Result(argv, 0, "7: vmbr1    inet 192.168.10.5/24 brd 192.168.10.255 scope global vmbr1\n")
                return Result(argv, 0, "default via 192.168.10.1 dev vmbr1 proto static\n")
        self.assertEqual(netutil.host_defaults(R(), "vmbr1"), {"subnet": "192.168.10.0/24", "gateway": "192.168.10.1"})

        class Other(R):
            def run(self, argv, **kw):
                return Result(argv, 0, "default via 10.0.0.1 dev vmbr0\n") if argv[2] == "route" else super().run(argv)
        self.assertEqual(netutil.host_defaults(Other(), "vmbr1"), {"subnet": "192.168.10.0/24"})  # other bridge's gateway
        self.assertEqual(netutil.host_defaults(StubRunner(Result([], 1)), "vmbr1"), {})


class StaticLifecycleTest(Base):
    def setUp(self):
        super().setUp()
        static(self.cfg)
        self.fake.vms[9000]["config"]["ipconfig0"] = "ip=dhcp"

    def vm_cfg(self, name):
        return self.fake.vms[self.db.get_project(name)["vmid"]]["config"]

    def test_create_assigns_distinct_addresses(self):
        self.lab().create("a")
        self.lab().create("b")
        self.assertEqual(self.db.get_project("a")["static_ip"], "192.168.10.200")
        self.assertEqual(self.db.get_project("b")["static_ip"], "192.168.10.201")
        cfg = self.vm_cfg("a")
        self.assertEqual(cfg["ipconfig0"], "ip=192.168.10.200/24,gw=192.168.10.1")
        self.assertEqual(cfg["nameserver"], "192.168.10.1 1.1.1.1")

    def test_pool_exhaustion_blocks_create_before_anything_happens(self):
        static(self.cfg, "192.168.10.200", "192.168.10.200")
        self.lab().create("a")
        with self.assertRaisesRegex(PreconditionError, "no free address"):
            self.lab().create("b")
        self.assertIsNone(self.db.get_project("b"))
        self.assertNotIn("lab-b", self.vm_names())

    def test_address_answering_a_ping_is_skipped(self):
        self.fake.ips_in_use = {"192.168.10.200"}
        self.lab().create("a")
        self.assertEqual(self.db.get_project("a")["static_ip"], "192.168.10.201")

    def test_failed_create_releases_the_address(self):
        self.fake.fail(("qm", "clone"))
        with self.assertRaises(Exception):
            self.lab().create("a")
        self.assertEqual(self.db.used_ips(), set())
        self.fake._failures.clear()
        self.lab().create("a")
        self.assertEqual(self.db.get_project("a")["static_ip"], "192.168.10.200")

    def test_address_survives_archive_restore_and_is_freed_by_delete(self):
        self.lab().create("a")
        self.lab().archive("a")
        row = self.db.get_project("a")
        self.assertEqual(row["static_ip"], "192.168.10.200")  # reserved while archived
        self.lab().create("b")
        self.assertEqual(self.db.get_project("b")["static_ip"], "192.168.10.201")
        self.lab().restore("a")
        self.assertEqual(self.vm_cfg("a")["ipconfig0"], "ip=192.168.10.200/24,gw=192.168.10.1")
        self.lab().delete("a", "a", archives=True)
        self.assertEqual(self.db.used_ips(), {"192.168.10.201"})

    def test_restore_after_switching_modes(self):
        self.lab().create("a")
        self.lab().archive("a")
        self.cfg.network.mode = "dhcp"
        self.lab().restore("a")
        self.assertEqual(self.vm_cfg("a")["ipconfig0"], "ip=dhcp")
        self.assertIsNone(self.db.get_project("a")["static_ip"])
        self.lab().archive("a")
        static(self.cfg)
        self.lab().restore("a")  # a project with no address gets one on its first static restore
        self.assertEqual(self.db.get_project("a")["static_ip"], "192.168.10.200")

    def test_dhcp_mode_is_unchanged(self):
        self.cfg.network.mode = "dhcp"
        self.lab().create("a")
        self.assertEqual(self.vm_cfg("a")["ipconfig0"], "ip=dhcp")
        self.assertNotIn("nameserver", self.vm_cfg("a"))
        self.assertIsNone(self.db.get_project("a")["static_ip"])


class StaticTemplateTest(unittest.TestCase):
    def test_build_vm_gets_a_pool_address_and_the_template_keeps_none(self):
        fake, io = FakeProxmox(), make_io()
        cfg = static(make_cfg())
        tb = TemplateBuilder(cfg, Proxmox(fake), io, DB(":memory:"), catalogue=CATALOGUE, sleep=lambda s: None)
        said = []
        tb.say = said.append
        vmid = tb.build("ubuntu-24.04")
        self.assertIn("build VM address: 192.168.10.200", said)
        applied = [c for c in fake.calls if c[:3] == ["qm", "set", str(vmid)] and "--ipconfig0" in c]
        self.assertEqual(applied[0][applied[0].index("--ipconfig0") + 1], "ip=192.168.10.200/24,gw=192.168.10.1")
        conf = fake.vms[vmid]["config"]
        self.assertNotIn("ipconfig0", conf)
        self.assertNotIn("nameserver", conf)
        self.assertNotIn("cicustom", conf)


class DiscoverPacketTest(unittest.TestCase):
    def test_discover_is_a_valid_dhcp_message(self):
        xid, mac = b"\x01\x02\x03\x04", b"\x02\xaa\xbb\xcc\xdd\xee"
        p = hostio.build_discover(xid, mac)
        self.assertEqual((p[0], p[1], p[2]), (1, 1, 6))  # BOOTREQUEST, ethernet, 6-byte hardware address
        self.assertEqual(p[4:8], xid)
        self.assertEqual(p[28:34], mac)
        self.assertEqual(p[236:240], b"\x63\x82\x53\x63")
        self.assertEqual(p[240:243], b"\x35\x01\x01")  # DHCPDISCOVER
        self.assertEqual(p[-1], 0xFF)
        self.assertEqual(p[10] & 0x80, 0x80)  # broadcast flag: reply reaches a host with no address

    def test_only_an_offer_for_our_transaction_counts(self):
        xid = b"\x01\x02\x03\x04"
        offer = bytearray(hostio.build_discover(xid, b"\x02" * 6))
        offer[0] = 2
        offer[242] = 2  # message type: DHCPOFFER
        self.assertTrue(hostio.is_offer(bytes(offer), xid))
        self.assertFalse(hostio.is_offer(bytes(offer), b"\x09\x09\x09\x09"))  # someone else's transaction
        self.assertFalse(hostio.is_offer(hostio.build_discover(xid, b"\x02" * 6), xid))  # our own request echoed
        self.assertFalse(hostio.is_offer(b"short", xid))


class DoctorAddressingTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.io, self.db = FakeProxmox(), make_io(), DB(":memory:")
        self.cfg = make_cfg()

    def check(self):
        px = Proxmox(self.fake)
        tb = TemplateBuilder(self.cfg, px, self.io, self.db, catalogue=CATALOGUE)
        return {c.name: c for c in doctor.run_checks(self.cfg, px, self.io, self.db, tb)}["addressing"]

    def test_dhcp_probe_results(self):
        for answer, level in ((True, doctor.OK), (None, doctor.OK), (False, doctor.WARN)):
            self.io.dhcp = answer
            self.assertEqual(self.check().level, level, answer)
        self.assertIn("lab init", self.check().hint)

    def test_static_pool_usage(self):
        static(self.cfg, "192.168.10.200", "192.168.10.204")
        self.assertEqual(self.check().level, doctor.OK)
        self.db.add_user("m@x.io", "max", "admin")
        for i in range(5):
            self.db.add_project(name=f"p{i}", vmid=1000 + i, owner="max", distro="d", cpu=1, ram_mb=1, disk_gb=1,
                                static_ip=f"192.168.10.{200 + i}")
        c = self.check()
        self.assertEqual(c.level, doctor.FAIL)
        self.assertIn("5/5", c.detail)


class InitAddressingTest(unittest.TestCase):
    def setUp(self):
        self.fake, self.io, self.db = FakeProxmox(), make_io(), DB(":memory:")
        self.io.files.pop("/etc/project-lab/ts-oauth")

    def run_init(self, answers, runner=None):
        ui = ScriptedPrompter({"Fast disk": "nvme", "Archive disk": "bigdisk", "Your tailnet login": "m@x.io", **answers},
                              assume_yes=True)
        res = init_mod.run_init(Proxmox(self.fake), self.io, runner or StubRunner(Result([], 1)), ui,
                                existing=config_mod.Config(), config_file="/etc/project-lab/config.toml",
                                db=self.db, env={})
        return res, ui

    def test_dhcp_found_defaults_to_dhcp(self):
        res, ui = self.run_init({})
        self.assertEqual(res.config.network.mode, "dhcp")
        self.assertTrue(any("DHCP server answered" in m for m in ui.log))

    def test_no_dhcp_defaults_to_static_and_asks_for_the_pool(self):
        self.io.dhcp = False
        res, ui = self.run_init({"Subnet": "192.168.10.0/24", "Gateway": "192.168.10.1", "DNS": "192.168.10.1",
                                 "First address": "192.168.10.200", "Last address": "192.168.10.220"})
        n = res.config.network
        self.assertEqual((n.mode, n.pool_start, n.pool_end), ("static", "192.168.10.200", "192.168.10.220"))
        self.assertTrue(any("No DHCP server answered" in m for m in ui.log))
        written = config_mod.from_dict(tomllib.loads(self.io.files["/etc/project-lab/config.toml"].decode()))
        self.assertEqual(written.network, n)

    def test_static_defaults_come_from_the_host_address_on_the_bridge(self):
        self.io.dhcp = False

        class R:
            def run(self, argv, **kw):
                if argv[2] == "-o":
                    return Result(argv, 0, "7: vmbr0 inet 10.1.2.5/24 scope global vmbr0\n")
                return Result(argv, 0, "default via 10.1.2.1 dev vmbr0\n")
        res, _ = self.run_init({"First address": "10.1.2.100", "Last address": "10.1.2.110"}, runner=R())
        n = res.config.network
        self.assertEqual((n.subnet, n.gateway, n.dns), ("10.1.2.0/24", "10.1.2.1", "10.1.2.1"))

    def test_bad_pool_is_rejected_and_nothing_is_written(self):
        self.io.dhcp = False
        with self.assertRaises(UsageError):
            self.run_init({"Subnet": "192.168.10.0/24", "Gateway": "192.168.10.1", "DNS": "1.1.1.1",
                           "First address": "192.168.10.50", "Last address": "10.9.9.9"})
        self.assertNotIn("/etc/project-lab/config.toml", self.io.files)

    def test_unprobeable_bridge_keeps_the_current_mode_as_default(self):
        self.io.dhcp = None
        res, ui = self.run_init({})
        self.assertEqual(res.config.network.mode, "dhcp")
        self.assertTrue(any("Could not probe" in m for m in ui.log))


class MigrationTest(unittest.TestCase):
    def test_static_ip_is_unique_but_null_is_not(self):
        db = DB(":memory:")
        db.add_user("m@x.io", "max", "admin")
        kw = dict(owner="max", distro="d", cpu=1, ram_mb=1, disk_gb=1)
        db.add_project(name="a", vmid=1, **kw)
        db.add_project(name="b", vmid=2, **kw)  # both NULL: fine
        db.add_project(name="c", vmid=3, static_ip="10.0.0.5", **kw)
        with self.assertRaises(Exception):
            db.add_project(name="d", vmid=4, static_ip="10.0.0.5", **kw)
        self.assertEqual(db.used_ips(), {"10.0.0.5"})


if __name__ == "__main__":
    unittest.main()
