from pathlib import Path
import unittest

class GeneratorAcceptance(unittest.TestCase):
    def test_drawing_program_and_safe_modes(self):
        p = Path(__file__).parent / 'job/1001.nc'
        self.assertTrue(p.exists(), 'Generated NC is missing')
        s = p.read_text()
        for token in ['O1001', 'G20', 'G54', 'G00 G17 G40 G49 G80 G90',
                      'T1 M06', 'T2 M06', 'G43 H1 Z1.', 'G43 H2 Z1.',
                      'G83', 'G80', 'M08', 'M30', 'Z-0.5000']:
            self.assertIn(token, s)
        self.assertNotIn('T3 M06', s, 'Drawing has no tapped holes')

if __name__ == '__main__':
    unittest.main()
