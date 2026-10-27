"""Contract test: run the real mock reply generator through the guardrails.

The other extraction tests use hand-written copies of the provider's replies;
this one imports ``provider/responses.py`` itself, so a change to the mock
breaks the suite instead of only the live run. It is skipped where the
provider sources are absent (the app image only contains ``app/``).
"""

import importlib.util
import random
import unittest
from collections import Counter
from pathlib import Path

import support  # noqa: F401
from extraction import ExtractionError, extract_notification
from models import FailureCode, Notification

RESPONSES_PY = Path(__file__).resolve().parents[2] / "provider" / "responses.py"

# (target, message, type) exactly as the mock derives them from the k6 prompts.
K6_FIELDS = [
    ("juan@example.com", "enviar email a juan@example.com diciendo que su pa", "email"),
    ("600-111-222", "avisar por sms al 600-111-222 que la reserva ha si", "sms"),
    ("marta@app.com", "tienes un nuevo mensaje.", "email"),
    ("699888777", "00", "sms"),
]
DRAWS = 5000
TERMINAL_CODES = {FailureCode.EXTRACTION_REFUSED, FailureCode.EXTRACTION_INVALID}


def load_generator(seed):
    spec = importlib.util.spec_from_file_location("provider_responses", RESPONSES_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.random = random.Random(seed)  # isolated, reproducible draws
    return module.generate_ai_response


@unittest.skipUnless(RESPONSES_PY.exists(), "provider sources not available")
class ProviderContractTest(unittest.TestCase):
    def test_every_reply_is_exact_or_a_terminal_rejection(self):
        generate = load_generator(seed=2026)
        outcomes = Counter()
        for n in range(DRAWS):
            fields = K6_FIELDS[n % len(K6_FIELDS)]
            content = generate(*fields)
            try:
                result = extract_notification(content)
            except ExtractionError as err:
                self.assertIn(err.code, TERMINAL_CODES, content)
                outcomes[err.code] += 1
            else:
                self.assertEqual(result, Notification(*fields), content)
                outcomes["sent"] += 1
        # The mock's distribution: 5/6 recoverable, 1/15 missing a field, 1/10 refusals.
        self.assertAlmostEqual(outcomes["sent"] / DRAWS, 0.833, delta=0.02)
        self.assertAlmostEqual(outcomes[FailureCode.EXTRACTION_INVALID] / DRAWS, 0.067, delta=0.015)
        self.assertAlmostEqual(outcomes[FailureCode.EXTRACTION_REFUSED] / DRAWS, 0.100, delta=0.015)

    def test_generator_has_exactly_the_sixteen_known_variants(self):
        generate = load_generator(seed=7)
        variants = {generate(*K6_FIELDS[0]) for _ in range(DRAWS)}
        self.assertEqual(len(variants), 16, "provider/responses.py gained or lost a reply shape")


if __name__ == "__main__":
    unittest.main()
