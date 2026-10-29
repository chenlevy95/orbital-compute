"""Run: python -m unittest discover -s tests -v"""

import io
from contextlib import redirect_stderr, redirect_stdout
import unittest

import pipeline


class PipelineTests(unittest.TestCase):
    def run_steps(self, *results):
        calls = []

        def step(name, result):
            def main():
                calls.append(name)
                if isinstance(result, Exception):
                    raise result
                return result
            return name, main

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = pipeline.run([step(f's{i}', r) for i, r in enumerate(results)])
        return code, calls

    def test_all_steps_succeed(self):
        self.assertEqual(self.run_steps(0, 0, 0), (0, ['s0', 's1', 's2']))

    def test_later_steps_still_run_after_a_failure(self):
        # A rejected workbook mustn't stop the models from being rebuilt.
        self.assertEqual(self.run_steps(0, 1, 0), (1, ['s0', 's1', 's2']))

    def test_an_exception_counts_as_a_failure(self):
        self.assertEqual(self.run_steps(RuntimeError('boom'), 0, None), (1, ['s0', 's1', 's2']))


if __name__ == '__main__':
    unittest.main()
