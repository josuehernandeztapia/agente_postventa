import unittest
from unittest import mock

import main


class MediaValidationTests(unittest.TestCase):
    def test_rejects_unsupported_image_type(self) -> None:
        ctype, reason = main._validate_media("https://example.com/img.heic", "image/heic")
        self.assertEqual(ctype, "image/heic")
        self.assertEqual(reason, "unsupported_image_type")

    def test_accepts_supported_audio(self) -> None:
        ctype, reason = main._validate_media("https://example.com/audio.mp3", "audio/mpeg")
        self.assertEqual(ctype, "audio/mpeg")
        self.assertIsNone(reason)


class MediaCachingTests(unittest.TestCase):
    def test_cached_media_skips_secondary_processing(self) -> None:
        media_item = {"url": "https://example.com/img.jpg", "content_type": "image/jpeg"}
        cache_payload = {
            'ocr': {'notes': 'cached'},
            'classification': {'part_guess': 'bujía'},
            'transcript': None,
            'provided_items': [],
            'recommended_checks': [],
            'oem_hits': [],
            'kind_guess': 'evidencia',
        }
        with mock.patch("main._media_cache_get", return_value=cache_payload), \
             mock.patch("main._media_cache_set"), \
             mock.patch("app.vision_openai.classify_part_image") as mocked_clf, \
             mock.patch("main._validate_media", return_value=("image/jpeg", None)):
            result = main._process_media_items("contact", [media_item], "general", None, {})
        mocked_clf.assert_not_called()
        self.assertTrue(result.get('part_class_list'))


if __name__ == "__main__":
    unittest.main()
