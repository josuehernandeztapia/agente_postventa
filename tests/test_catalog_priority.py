import unittest
from unittest import mock

import main


class CatalogPriorityTests(unittest.TestCase):
    def test_prepend_catalog_hits_with_oem(self) -> None:
        docs = [{'metadata': {'source': 'manual', 'doc_id': 'doc', 'page_label': '10'}}]
        cat = {'id': 'catalog:ABC-123', 'metadata': {'source': 'catalog', 'oem': 'ABC-123', 'doc_id': 'parts_catalog', 'page_label': '200'}}
        with mock.patch('main._catalog_hits_for_question', return_value=[cat]):
            result = main._prepend_catalog_hits('OEM ABC-123', docs)
        self.assertEqual(result[0]['id'], 'catalog:ABC-123')


if __name__ == '__main__':
    unittest.main()
