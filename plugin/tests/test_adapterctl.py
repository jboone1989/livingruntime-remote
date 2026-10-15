import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from adapterctl import change

class AdapterCtlTests(unittest.TestCase):
    def test_install_list_uninstall(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"manifest.json"
            self.assertEqual(change("list",path=p)["adapters"],[])
            change("install","livingruntime_adapter_mock","make",p)
            self.assertEqual(change("list",path=p)["adapters"][0]["factory"],"make")
            with self.assertRaises(ValueError):
                change("install","livingruntime_adapter_mock","make",p)
            with self.assertRaises(ValueError):
                change("install","os","system",p)
            change("uninstall","livingruntime_adapter_mock",path=p)
            self.assertEqual(change("list",path=p)["adapters"],[])
            with self.assertRaises(KeyError):
                change("uninstall","livingruntime_adapter_mock",path=p)
if __name__=="__main__":
    unittest.main()
