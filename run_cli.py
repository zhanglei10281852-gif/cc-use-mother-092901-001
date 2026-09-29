import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from permit_coordination.contracts import PermitApplication, PermitState, RouteWindow


window = RouteWindow("R-12", datetime(2026, 10, 21, 1, tzinfo=timezone.utc), datetime(2026, 10, 21, 3, tzinfo=timezone.utc))
application = PermitApplication("AP-1", 2, ("VIN-01",), ("DRV-01",), (window,), PermitState.REVIEW)
print(json.dumps({"application_id": application.application_id, "version": application.version, "state": application.state.value}, ensure_ascii=False))
