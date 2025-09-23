import asyncio
import os
import unittest
from urllib.parse import urlencode
from unittest import mock

from fastapi import HTTPException
from starlette.requests import Request

import main


def _build_form_request(path: str, data: dict[str, str], signature: str | None = None) -> Request:
    body = urlencode(data, doseq=True).encode("utf-8")
    headers = [
        (b"content-type", b"application/x-www-form-urlencoded"),
        (b"content-length", str(len(body)).encode("utf-8")),
    ]
    if signature is not None:
        headers.append((b"x-twilio-signature", signature.encode("utf-8")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": path,
        "headers": headers,
        "query_string": b"",
    }

    state = {"body": body}

    async def receive() -> dict:
        if state["body"] is not None:
            chunk = state["body"]
            state["body"] = None
            return {"type": "http.request", "body": chunk, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


class DetectTopicSwitchTests(unittest.TestCase):
    def test_keyword_triggers_switch(self) -> None:
        self.assertTrue(main._detect_topic_switch("Traigo otro tema"))

    def test_brand_classification_triggers(self) -> None:
        cls_list = [{"brand": "ACME"}]
        self.assertTrue(main._detect_topic_switch("seguimos?", cls_list=cls_list))

    def test_transcript_triggers_switch(self) -> None:
        transcripts = ["No es problema anterior, es otro tema"]
        self.assertTrue(main._detect_topic_switch("hola", transcripts=transcripts))

    def test_no_trigger_returns_false(self) -> None:
        self.assertFalse(main._detect_topic_switch("Continuamos con lo mismo"))

    def test_mixed_media_inputs(self) -> None:
        ocr_list = [{"notes": "Garrafa refrigerante Valucraft"}]
        transcripts = ["Checa la imagen que mandé"]
        self.assertTrue(main._detect_topic_switch("Gracias", ocr_list=ocr_list, transcripts=transcripts))


class TwilioValidationTests(unittest.TestCase):
    def test_validation_enabled_by_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(main._twilio_validation_enabled())

    def test_validation_can_be_disabled(self) -> None:
        with mock.patch.dict(os.environ, {"TWILIO_VALIDATE": "0"}, clear=True):
            self.assertFalse(main._twilio_validation_enabled())

    def test_enforce_signature_accepts_valid_request(self) -> None:
        token = "abcd1234abcd1234abcd1234abcd1234"
        data = {"Body": "hola", "From": "+521234", "NumMedia": "0"}
        path = "/twilio/whatsapp"
        base_url = "https://example.com"
        params = sorted((k, str(v)) for k, v in data.items())
        expected = main._twilio_expected_signature(token, f"{base_url}{path}", params)
        request = _build_form_request(path, data, signature=expected)
        with mock.patch.dict(
            os.environ,
            {"PUBLIC_BASE_URL": base_url, "TWILIO_AUTH_TOKEN": token},
            clear=False,
        ):
            asyncio.run(main._enforce_twilio_signature(request, path))

    def test_enforce_signature_rejects_invalid_signature(self) -> None:
        token = "abcd1234abcd1234abcd1234abcd1234"
        data = {"Body": "hola", "From": "+521234", "NumMedia": "0"}
        path = "/twilio/whatsapp"
        base_url = "https://example.com"
        request = _build_form_request(path, data, signature="bad-signature")
        with mock.patch.dict(
            os.environ,
            {"PUBLIC_BASE_URL": base_url, "TWILIO_AUTH_TOKEN": token},
            clear=False,
        ):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(main._enforce_twilio_signature(request, path))
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, "twilio signature invalid")

    def test_enforce_signature_disabled_skips_validation(self) -> None:
        data = {"Body": "hola", "From": "+521234", "NumMedia": "0"}
        request = _build_form_request("/twilio/whatsapp", data, signature="whatever")
        with mock.patch.dict(
            os.environ,
            {"TWILIO_VALIDATE": "false"},
            clear=False,
        ):
            asyncio.run(main._enforce_twilio_signature(request, "/twilio/whatsapp"))

    def test_missing_config_raises(self) -> None:
        data = {"Body": "hola"}
        request = _build_form_request("/twilio/whatsapp", data, signature="sig")
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(main._enforce_twilio_signature(request, "/twilio/whatsapp"))
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, "twilio signature invalid (config)")


class SystemPromptHybridTests(unittest.TestCase):
    def test_critical_prompt_includes_alert_and_evidence(self) -> None:
        prompt = main.system_prompt_hybrid(
            severity="critical",
            categories=["brakes"],
            missing_evidence=["foto_vin"],
        )
        self.assertIn("⚠️ ALERTA", prompt)
        self.assertIn("Categoría frenos", prompt)
        self.assertIn("Evidencia pendiente: enumera foto_vin", prompt)

    def test_normal_prompt_defaults_when_no_category(self) -> None:
        prompt = main.system_prompt_hybrid(severity="normal", categories=[], missing_evidence=None)
        self.assertIn("Resumen:", prompt)
        self.assertIn("Aterriza los pasos al sistema detectado", prompt)


class MaskingTests(unittest.TestCase):
    def test_mask_vin(self) -> None:
        self.assertEqual(main._mask_vin("ABCDEFGHIJKLM"), "*********JKLM")

    def test_sanitize_ocr_for_log(self) -> None:
        payload = {"vin": "ABC12345", "plate": "XYZ-987", "odo_km": 1000, "evidence_type": "vin_plate"}
        sanitized = main._sanitize_ocr_for_log(payload)
        self.assertEqual(sanitized['vin'], "****2345")
        self.assertEqual(sanitized['plate'], "****987")


if __name__ == "__main__":
    unittest.main()
