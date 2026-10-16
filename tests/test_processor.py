import unittest

import numpy as np
from moviepy.audio.AudioClip import AudioArrayClip

from mtslinker.processor import create_audio_with_gaps


class AudioGapTests(unittest.TestCase):
    def make_audio(self, start):
        clip = AudioArrayClip(np.full((8000, 2), 0.25), fps=8000).with_start(start)
        self.addCleanup(clip.close)
        return clip

    def test_subsample_gaps(self):
        gap = 0.00001  # Positive, but smaller than one sample at 8000 Hz.
        cases = (
            ("leading", 1 + gap, [gap]),
            ("between clips", 2 + gap, [0, 1 + gap]),
            ("trailing", 1 + gap, [0]),
            ("rounding between clips", 2.0, [0, np.nextafter(1.0, 2.0)]),
        )
        for name, duration, starts in cases:
            with self.subTest(name=name):
                clips = [self.make_audio(start) for start in starts]
                result = create_audio_with_gaps(duration, clips)
                self.addCleanup(result.close)
                self.assertAlmostEqual(result.duration, duration, delta=1 / 8000)
                np.testing.assert_allclose(result.get_frame(starts[0] + 0.5), [0.25, 0.25])

    def test_regular_gap_is_silent(self):
        result = create_audio_with_gaps(2.0, [self.make_audio(1.0)])
        self.addCleanup(result.close)
        self.assertEqual(result.duration, 2.0)
        np.testing.assert_allclose(result.get_frame(0.5), [0.0, 0.0])
        np.testing.assert_allclose(result.get_frame(1.5), [0.25, 0.25])


if __name__ == "__main__":
    unittest.main()
