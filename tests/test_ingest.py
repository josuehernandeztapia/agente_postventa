import unittest
from types import SimpleNamespace

from app.ingesta_unificada import _extract_page_number, _filter_documents_by_pages


class IngestHelpersTests(unittest.TestCase):
    def test_extract_page_number_handles_various_keys(self) -> None:
        meta_cases = [
            ({'page_number': '12'}, 12),
            ({'page_index': 0}, 1),
            ({'page_label': '003'}, 3),
            ({'page': 5}, 5),
        ]
        for meta, expected in meta_cases:
            self.assertEqual(_extract_page_number(meta), expected)

    def test_filter_documents_by_pages(self) -> None:
        docs = [
            SimpleNamespace(metadata={'page_number': '1'}),
            SimpleNamespace(metadata={'page_label': '2'}),
            SimpleNamespace(metadata={'page_number': '5'}),
        ]
        filtered = _filter_documents_by_pages(docs, {2, 5})
        self.assertEqual(len(filtered), 2)
        self.assertEqual(_extract_page_number(filtered[0].metadata), 2)
        self.assertEqual(_extract_page_number(filtered[1].metadata), 5)


if __name__ == '__main__':
    unittest.main()
