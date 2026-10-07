from pathlib import Path
import unittest

class GeneratorContract(unittest.TestCase):
    def test_generator_and_program_contract(self):
        root = Path(__file__).resolve().parent
        self.assertTrue((root / 'gen.py').exists(), 'Generator not yet implemented')
        nc = (root / 'job/1001.nc').read_text()
        for token in ('O1001', 'G20', 'G54', 'G00 G17 G40 G49 G80 G90',
                      'T1 M06', 'T2 M06', 'G43 H1 Z1.', 'G43 H2 Z1.',
                      'M08', 'G83', 'G80', 'M30'):
            self.assertIn(token, nc)
        self.assertNotIn('T3 M06', nc)
        self.assertIn('Z-1.1000', nc)
        self.assertIn('Z-0.5000', nc)

if __name__ == '__main__':
    unittest.main()
