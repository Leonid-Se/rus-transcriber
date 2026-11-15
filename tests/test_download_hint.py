import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import download_hint


class DownloadHintTests(unittest.TestCase):
    def test_recognizes_common_certificate_errors(self):
        cases = (
            "CERTIFICATE_VERIFY_FAILED",
            "certificate has expired",
            "unable to get local issuer certificate",
            "SSLCertVerificationError",
            "Истек/не наступил срок действия сертификата",
        )
        for message in cases:
            with self.subTest(message=message):
                self.assertTrue(download_hint.is_certificate_error(message))

    def test_does_not_match_unrelated_errors(self):
        self.assertFalse(download_hint.is_certificate_error("HTTP Error 404: Not Found"))
        self.assertFalse(download_hint.is_certificate_error("Connection timed out"))

    def test_hint_links_documentation_only_for_certificate_errors(self):
        hint = download_hint.certificate_hint(
            "URLError: certificate verify failed: certificate has expired"
        )
        self.assertIn(download_hint.DOC, hint)
        self.assertIn("download_hint.py", hint)
        self.assertEqual("", download_hint.certificate_hint("HTTP 404"))

    def test_manual_commands_are_explicit_http_and_verify_hashes(self):
        script = download_hint.manual_download_script(r"C:\rus-transcriber")
        self.assertIn(
            "http://cdn.chatwm.opensmodel.sberdevices.ru/GigaAM/$name", script
        )
        self.assertIn("Get-FileHash $part -Algorithm MD5", script)
        for digest in download_hint.HASHES.values():
            self.assertIn(digest, script)
        self.assertIn("md5 не совпал", script)
        self.assertNotIn("verify=False", script)

    def test_check_commands_use_given_installation_root(self):
        script = download_hint.check_script(r"D:\apps\rus-transcriber")
        self.assertIn(r"D:\apps\rus-transcriber\.venv\Scripts\python.exe", script)
        self.assertIn("gigaam.load_model('emo'", script)
        self.assertIn("gigaam.load_model('v3_e2e_rnnt'", script)

    def test_reads_model_hashes_from_installed_gigaam_source(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / ".venv" / "Lib" / "site-packages" / "gigaam" / "__init__.py"
            source.parent.mkdir(parents=True)
            source.write_text(
                '_MODEL_HASHES = {"emo": "' + "a" * 32
                + '", "v3_e2e_rnnt": "' + "b" * 32 + '"}\n',
                encoding="utf-8",
            )
            hashes = download_hint.expected_hashes(tmp)
        self.assertEqual("a" * 32, hashes["emo.ckpt"])
        self.assertEqual("b" * 32, hashes["v3_e2e_rnnt.ckpt"])


if __name__ == "__main__":
    unittest.main()
