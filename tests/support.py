import json
from pathlib import Path
import tempfile
import unittest

from formaggio.config import ROOT, load_json
from formaggio.shop.data_models import LineItem, Request
from formaggio.operations.observability import Recorder


def fixture(name="standard", **changes):
    scenario = next(s for s in load_json("scenarios.json") if s["id"] == name)
    request = Request.model_validate({**scenario["request"], **changes})
    proposals = json.loads((ROOT / "tests/fixtures/proposals.json").read_text())
    return request, [LineItem.model_validate(i) for i in proposals[name]]


class RecordingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.recorder = Recorder(self.root / "test.sqlite", secrets=["unit-test-credential"])
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.recorder.close)
        self.version = self.recorder.version({"prompt": "test"})
        self.run_id = self.recorder.start_run(self.version)
