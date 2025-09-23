import unittest

import build_parts_catalog as catalog


class CleanNameTests(unittest.TestCase):
    def test_clean_name_removes_notes_and_normalizes(self) -> None:
        raw = "BALATA DELANTERA (ver nota 1) Ref. ABC-123"
        cleaned = catalog._clean_name(raw)
        self.assertEqual(cleaned, "Balata Delantera")


if __name__ == '__main__':
    unittest.main()
