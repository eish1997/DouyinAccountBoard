import hashlib
import json
import unittest
from uuid import uuid4
import zipfile
import server_package
from test_isolation import checked_root


class PackageTest(unittest.TestCase):
    def test_exact_allowlist_and_hashes(self):
        target = checked_root() / ('source-' + uuid4().hex + '.zip')
        expected = server_package.build(target)
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(set(archive.namelist()), set(server_package.FILES) |
                {'SOURCE-MANIFEST.json', 'RELEASE-STATUS.txt'})
            manifest = json.loads(archive.read('SOURCE-MANIFEST.json'))
            self.assertEqual(manifest, expected)
            for name in server_package.FILES:
                self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(), manifest[name])
                self.assertFalse(name.startswith(('data/', 'backups/', 'artifacts/', 'desktop/')))
        with self.assertRaises(ValueError):
            server_package.build(target)
