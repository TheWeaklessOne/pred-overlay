"""errors.describe: what an exception may say in the log."""

import unittest
from pathlib import Path
from unittest import mock

from predoverlay.errors import TransportError, describe, stack


def raised(exc):
    try:
        raise exc
    except Exception as caught:
        return caught


class DescribeTests(unittest.TestCase):
    def test_a_message_that_could_quote_a_name_is_left_out(self):
        exc = raised(KeyError("SomePlayer"))
        text = describe(exc)
        self.assertTrue(text.startswith("KeyError at test_errors.py:"), text)
        self.assertNotIn("SomePlayer", text)
        self.assertNotIn("SomePlayer", stack(exc))
        self.assertIn("test_errors.py", stack(exc))

    def test_the_apps_own_and_system_errors_keep_their_message(self):
        self.assertEqual(
            describe(raised(TransportError("Pred.gg is unreachable."))),
            "TransportError: Pred.gg is unreachable.",
        )
        self.assertEqual(describe(raised(OSError("disk full"))), "OSError: disk full")
        self.assertEqual(describe(raised(RuntimeError("No OCR"))), "RuntimeError: No OCR")
        self.assertNotIn("Someone", describe(raised(ValueError("bad name Someone"))))

    def test_the_place_is_the_apps_own_line_not_the_librarys(self):
        import json

        from predoverlay import errors

        with mock.patch.object(errors, "_PACKAGE", Path(__file__).resolve().parent):
            try:
                json.loads("{")
            except ValueError as exc:
                text = describe(exc)
        self.assertTrue(text.startswith("JSONDecodeError at test_errors.py:"), text)

    def test_the_exceptions_that_led_to_it_are_in_the_stack(self):
        name = "".join(("Some", "one"))  # a value, not text in this file's source lines
        try:
            try:
                {}[name]
            except KeyError as inner:
                raise ValueError(name) from inner
        except ValueError as exc:
            text = stack(exc)
        self.assertIn("caused by KeyError at test_errors.py:", text)
        self.assertNotIn(name, text)

    def test_none_is_described_without_raising(self):
        self.assertEqual(describe(None), "an unknown error")
        self.assertEqual(stack(None), "")

    def test_an_exception_never_raised_has_no_place(self):
        self.assertEqual(describe(ValueError("x")), "ValueError")
        self.assertEqual(stack(ValueError("x")), "")


if __name__ == "__main__":
    unittest.main()
