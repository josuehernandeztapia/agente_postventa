import os
import tempfile
import unittest

from app import storage


class CaseStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_log_dir = storage.LOG_DIR
        self.old_case_state = storage.CASE_STATE_FILE
        self.old_case_log = storage.CASE_LOG_FILE
        self.old_media_queue = storage.MEDIA_QUEUE_FILE
        storage.LOG_DIR = self.tmp.name
        storage.CASE_STATE_FILE = os.path.join(self.tmp.name, 'cases_state.json')
        storage.CASE_LOG_FILE = os.path.join(self.tmp.name, 'cases.jsonl')
        storage.MEDIA_QUEUE_FILE = os.path.join(self.tmp.name, 'media_queue.jsonl')
        storage._ensure_log_path()

    def tearDown(self) -> None:
        storage.LOG_DIR = self.old_log_dir
        storage.CASE_STATE_FILE = self.old_case_state
        storage.CASE_LOG_FILE = self.old_case_log
        storage.MEDIA_QUEUE_FILE = self.old_media_queue
        self.tmp.cleanup()

    def test_required_provided_logs_with_timestamps(self) -> None:
        contact = 'test-contact'
        storage.add_required(contact, ['foto_vin'])
        storage.add_required(contact, ['foto_vin', 'foto_odometro'])
        storage.mark_provided(contact, ['foto_vin'])
        case = storage.get_case_state(contact)
        req_log = case.get('required_log', {})
        prov_log = case.get('provided_log', {})
        self.assertIn('foto_vin', req_log)
        self.assertIn('first_requested_at', req_log['foto_vin'])
        self.assertIn('fulfilled_at', req_log['foto_vin'])
        self.assertIn('foto_vin', prov_log)
        self.assertIn('first_provided_at', prov_log['foto_vin'])

    def test_attach_media_dedupes_by_url(self) -> None:
        contact = 'media-contact'
        storage.attach_media(contact, [{'url': 'https://example.com/a.jpg', 'content_type': 'image/jpeg'}])
        storage.attach_media(contact, [{'url': 'https://example.com/a.jpg', 'content_type': 'image/jpeg'}])
        case = storage.get_case_state(contact)
        attachments = case.get('attachments')
        self.assertEqual(len(attachments), 1)
        self.assertIn('first_seen_at', attachments[0])
        self.assertIn('url_hash', attachments[0])

    def test_case_requirements_snapshot_includes_details(self) -> None:
        contact = 'snapshot-contact'
        storage.add_required(contact, ['foto_vin'])
        snap = storage.case_requirements_snapshot(contact)
        self.assertIn('required_details', snap)
        self.assertTrue(any(d.get('item') == 'foto_vin' for d in snap.get('required_details', [])))


if __name__ == '__main__':
    unittest.main()
