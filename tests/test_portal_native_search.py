import os
import threading
import subprocess
import sys
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from corpus_portal.isolated_search import IsolatedSearchEngine

@unittest.skipUnless(os.getenv("CORPUS_PORTAL_NATIVE_TEST") == "1", "explicit native BCC opt-in required")
class NativeSharedSearchTests(unittest.TestCase):
    def test_two_indexes_remain_isolated_when_alternating_queries(self):
        with TemporaryDirectory(prefix="bcc-share-") as directory:
            root = Path(directory)
            slots = threading.BoundedSemaphore(2)
            engines = []
            for index, word in enumerate(["甲组专用电芯", "乙组专用光伏"]):
                source = root / f"source-{index}.txt"
                source.write_text(f"Doc test{index}\n" + (f"#Global 来源=验收\n#Group\nItem: (ROOT(NP(n {word}))(VP(v 发展))(w(x 。)))\n" * 50), encoding="gb18030")
                target = root / f"index-{index}"
                built = subprocess.run([sys.executable, "-m", "corpus_search.index_worker", str(target)],
                               input=json.dumps({"sources": [str(source)], "structure": "HZ"}),
                               text=True, capture_output=True, timeout=60)
                self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
                engines.append(IsolatedSearchEngine(target, slots))
            for index in [0, 1, 0, 1]:
                own = ["甲组专用电芯", "乙组专用光伏"][index]
                other = ["乙组专用光伏", "甲组专用电芯"][index]
                result = engines[index].frequency(own)
                self.assertEqual(result["Freq"].get(own), 50)
                self.assertEqual(engines[index].frequency(other)["Count"], 0)
                self.assertGreater(engines[index].kwic(own)["Count"], 0)

if __name__ == "__main__":
    unittest.main()
