import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "onbox" / "arista7050_web.py"


def load_module():
    spec = importlib.util.spec_from_file_location("arista7050_web", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


web = load_module()


class ParserTests(unittest.TestCase):
    def test_environment_reports_power_loss_for_installed_second_psu(self):
        output = """
System temperature status is: Ok
System cooling status is: Ok
Ambient temperature: 24C
Fan            Status  Speed
-------------- ------ ------
1/1            Ok        29%
Power                        Input  Output  Output
Supply Model       Capacity Current Current  Power Status      Uptime
------ ----------- -------- ------- ------- ------ ---------- -------
1      PWR-500AC-F     500W   0.43A   6.78A  82.8W Ok         2:10:09
2      PWR-500AC-F     500W   0.00A   0.00A   0.0W Power Loss Offline
"""
        health = web.parse_environment(output)
        self.assertEqual(health["fanStatus"], "OK")
        self.assertEqual(health["psuStatus"], "CHECK")
        self.assertIn("PSU2 power loss offline", health["psuDetails"])

    def test_environment_single_present_psu_is_ok(self):
        output = """
System temperature status is: Ok
System cooling status is: Ok
Ambient temperature: 24C
Power                        Input  Output  Output
Supply Model       Capacity Current Current  Power Status      Uptime
------ ----------- -------- ------- ------- ------ ---------- -------
1      PWR-500AC-F     500W   0.43A   6.78A  82.8W Ok         2:10:09
"""
        health = web.parse_environment(output)
        self.assertEqual(health["psuStatus"], "OK")

    def test_lldp_json_keeps_system_name_and_port_id(self):
        data = {
            "lldpNeighbors": {
                "Ethernet2": {
                    "lldpNeighborInfo": [
                        {
                            "systemName": "SE106 Pro",
                            "chassisId": "4cb7.e052.f32c",
                            "ttl": 120,
                            "neighborInterfaceInfo": {"interfaceId_v2": "6"},
                            "managementAddresses": [{"address": "192.168.31.119"}],
                        }
                    ]
                }
            }
        }
        rows = web.parse_lldp_json(data)
        self.assertEqual(rows[0]["neighbor"], "SE106 Pro")
        self.assertEqual(rows[0]["neighborPort"], "6")
        self.assertEqual(rows[0]["managementAddress"], "192.168.31.119")

    def test_transceiver_json_combines_dom_and_inventory(self):
        summary = {
            "interfaces": {
                "Ethernet2": {
                    "mediaType": "10GBASE-SR",
                    "temperature": 27.6,
                    "txPower": -3.0,
                    "rxPower": -2.54,
                    "vendorSn": "U7M89P17851",
                }
            }
        }
        inventory = {
            "xcvrSlots": {
                "2": {
                    "mfgName": "Hisense",
                    "modelName": "LTF8502-BC+",
                    "serialNum": "U7M89P17851",
                }
            }
        }
        modules = web.parse_transceivers_json(summary, {}, {}, inventory)
        et2 = modules["ethernet2"]
        self.assertTrue(et2["present"])
        self.assertEqual(et2["vendor"], "Hisense")
        self.assertEqual(et2["model"], "LTF8502-BC+")
        self.assertEqual(et2["serial"], "U7M89P17851")
        self.assertEqual(et2["type"], "10GBASE-SR")


class ConfigValidationTests(unittest.TestCase):
    def test_svi_rejects_network_and_broadcast_addresses(self):
        with self.assertRaisesRegex(ValueError, "network or broadcast"):
            web.build_config_action("svi_interface", {"vlan": "10", "address": "192.168.10.0/24"})
        with self.assertRaisesRegex(ValueError, "network or broadcast"):
            web.build_config_action("svi_interface", {"vlan": "10", "address": "192.168.10.255/24"})

    def test_svi_accepts_gateway_address(self):
        commands = web.build_config_action("svi_interface", {"vlan": "10", "address": "192.168.10.1/24", "description": "users"})
        self.assertEqual(commands, ["interface Vlan10", "ip address 192.168.10.1/24", "description users"])


if __name__ == "__main__":
    unittest.main()
