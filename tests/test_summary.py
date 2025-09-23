import os
import unittest
from unittest import mock

import main


class SummaryTests(unittest.TestCase):
    def test_extractive_summary_skips_llm(self) -> None:
        long_text = "La bomba presenta fuga. Revisar mangueras." * 3
        with mock.patch('main.ChatOpenAI') as mocked_llm, \
             mock.patch.dict(os.environ, {'SUMMARY_FORCE_LLM': '0'}, clear=False):
            result = main._summarize_to_limit(long_text, "", [], 120)
        self.assertLessEqual(len(result), 120)
        mocked_llm.assert_not_called()


if __name__ == '__main__':
    unittest.main()
