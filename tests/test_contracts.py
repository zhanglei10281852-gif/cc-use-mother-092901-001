import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from permit_coordination.contracts import PermitApplication, PermitState, RouteWindow


class PermitContractTests(unittest.TestCase):
    def test_application_keeps_versioned_scope(self):
        window = RouteWindow("R-1", datetime(2026, 10, 21, tzinfo=timezone.utc), datetime(2026, 10, 21, 1, tzinfo=timezone.utc))
        item = PermitApplication("AP-7", 3, ("V-1",), ("D-1",), (window,), PermitState.SIGNED)
        self.assertEqual((item.version, item.state.value), (3, "signed"))

    def test_invalid_window_is_rejected(self):
        instant = datetime(2026, 10, 21, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            RouteWindow("R-2", instant, instant)


if __name__ == "__main__":
    unittest.main()
