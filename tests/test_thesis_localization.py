import importlib.util
import unittest

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


class ThesisLocalizationTests(unittest.TestCase):
    def test_english_localizer_translates_labels_and_uses_english_suffix(self) -> None:
        spec = importlib.util.find_spec("tools.l4s.thesis_localization")
        self.assertIsNotNone(spec, "the shared thesis localization module is missing")

        from tools.l4s.thesis_localization import FigureLocalizer

        text = FigureLocalizer("en")
        self.assertEqual(text("تاخیر صف (میلی‌ثانیه)"), "Queue delay (ms)")
        self.assertEqual(text("Prague / ECT(1)"), "Prague / ECT(1)")
        self.assertEqual(
            text.stem("figure-06-queue-delay-cdf-fa"),
            "figure-06-queue-delay-cdf-en",
        )
        self.assertEqual(
            text.stem("figure-06-queue-delay-cdf-fa.pdf"),
            "figure-06-queue-delay-cdf-en.pdf",
        )

    def test_english_localizer_rejects_untranslated_persian(self) -> None:
        spec = importlib.util.find_spec("tools.l4s.thesis_localization")
        self.assertIsNotNone(spec, "the shared thesis localization module is missing")

        from tools.l4s.thesis_localization import FigureLocalizer

        with self.assertRaisesRegex(KeyError, "missing English translation"):
            FigureLocalizer("en")("برچسب آزمایشی ترجمه‌نشده")

    def test_english_topology_contains_only_english_visible_text(self) -> None:
        from tools.l4s.thesis_localization import FigureLocalizer, PERSIAN_RE
        from tools.l4s.thesis_topology import draw_coexistence_topology

        figure, axis = plt.subplots()
        try:
            draw_coexistence_topology(axis, FigureLocalizer("en"))
        except KeyError as error:
            self.fail(str(error))
        finally:
            plt.close(figure)

        labels = [label.get_text() for label in axis.texts]
        self.assertFalse(any(PERSIAN_RE.search(label) for label in labels))
        self.assertTrue(any("Shared bottleneck" in label for label in labels))


if __name__ == "__main__":
    unittest.main()
