import unittest
from unittest import mock

from app import storage


class ExtractSignalsTests(unittest.TestCase):
    def test_detects_brakes_and_critical(self) -> None:
        text = "La vagoneta se quedó sin frenos y huele a quemado"
        result = storage.extract_signals(text)

        self.assertEqual(result.get("category"), "brakes")
        self.assertEqual(result.get("severity"), "critical")
        self.assertIn("length", result)

    def test_dtc_catalog_adjusts_category_and_severity(self) -> None:
        fake_catalog = {
            "P0123": {
                "description": "Sensor A",
                "category": "electrical",
                "severity": "urgent",
            }
        }
        with mock.patch("app.storage._load_dtc_catalog", return_value=fake_catalog):
            result = storage.extract_signals("Código P0123 sin más datos")

        self.assertIn("P0123", result.get("dtc_codes", []))
        self.assertEqual(result.get("category"), "electrical")
        self.assertEqual(result.get("severity"), "urgent")
        self.assertTrue(any(d.get("category") == "electrical" for d in result.get("dtc_details", [])))

    def test_detects_model_and_problem(self) -> None:
        text = "Mi h6c presenta vibración y fuga de aceite"
        result = storage.extract_signals(text)

        self.assertEqual(result.get("model"), "H6C")
        self.assertEqual(result.get("problem"), "leak")
        self.assertEqual(result.get("severity"), "urgent")

    def test_detects_oil_code_and_oil_category(self) -> None:
        text = "Marcó OIL 003 y la presión de aceite baja"
        result = storage.extract_signals(text)

        self.assertEqual(result.get("oil_code"), "003")
        self.assertEqual(result.get("category"), "oil")
        self.assertEqual(result.get("severity"), "critical")

    def test_detects_ac_category(self) -> None:
        text = "El aire acondicionado deja de enfriar y el evaporador suda"
        result = storage.extract_signals(text)

        self.assertEqual(result.get("category"), "ac")
        self.assertEqual(result.get("severity"), "normal")

    def test_detects_body_category(self) -> None:
        text = "Hay corrosión en la carrocería y pintura levantada"
        result = storage.extract_signals(text)

        self.assertEqual(result.get("category"), "body")
        self.assertEqual(result.get("severity"), "normal")


if __name__ == "__main__":
    unittest.main()
