from unittest import TestCase

from merge_venv.ui import project_conflict_rows_as_tsv


class UiTests(TestCase):
    def test_formats_project_conflicts_as_clipboard_table(self):
        result = project_conflict_rows_as_tsv(
            (
                ("C:/projects/one", "Python", "3.11.8"),
                ("C:/projects/two", "NumPy", "2.0.0"),
            )
        )

        self.assertEqual(
            result,
            "Project folder\tConflicting dependency\tDetected version\r\n"
            "C:/projects/one\tPython\t3.11.8\r\n"
            "C:/projects/two\tNumPy\t2.0.0\r\n",
        )

    def test_replaces_control_characters_that_would_break_the_table(self):
        result = project_conflict_rows_as_tsv((("project\tname", "NumPy", "2.0\nlocal"),))

        self.assertIn("project name\tNumPy\t2.0 local", result)
