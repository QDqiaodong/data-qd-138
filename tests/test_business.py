
import copy,json,unittest,tempfile,subprocess,sys,hashlib,random,itertools
from pathlib import Path
from fractions import Fraction
from raster_objects.model import analyze
ROOT=Path(__file__).resolve().parents[1]
def sample():return json.loads((ROOT/'fixtures/input.json').read_text())
class Business(unittest.TestCase):
    def bad(self,d):
        with self.assertRaises((ValueError,TypeError)):analyze(d)
    def test_unknown_fields(self):
        d=sample();d['unknown']=1;self.bad(d)

    def test_objects(self):
        r=analyze(sample());self.assertEqual([o['cells'] for o in r['objects']],[3,2,1]);self.assertEqual([o['perimeter_edges'] for o in r['objects']],[8,6,4]);self.assertEqual(r['objects'][0]['centroid_row'],'1/3');self.assertEqual((r['foreground_cells'],r['background_cells'],r['unknown_cells']),(6,5,1));self.assertIsNone(r['labels'][1][2])
    def test_diagonal(self):
        d={'connectivity':4,'grid':[[1,0],[0,1]]};self.assertEqual(len(analyze(d)['objects']),2);d['connectivity']=8;r=analyze(d);self.assertEqual(len(r['objects']),1);self.assertEqual(r['objects'][0]['perimeter_edges'],8)
    def test_unknown(self):
        r=analyze({'connectivity':4,'grid':[[None,None]]});self.assertEqual(r['unknown_cells'],2);self.assertEqual(r['objects'],[])
    def test_ring_perimeter(self):
        r=analyze({'connectivity':4,'grid':[[1,1,1],[1,0,1],[1,1,1]]});self.assertEqual(r['objects'][0]['perimeter_edges'],16)
    def test_ragged(self):self.bad({'connectivity':4,'grid':[[1],[1,0]]})
    def test_bool(self):self.bad({'connectivity':4,'grid':[[True]]})
    def test_connectivity(self):self.bad({'connectivity':6,'grid':[[1]]})
    def test_all_background(self):self.assertEqual(analyze({'connectivity':4,'grid':[[0]]})['labels'],[[0]])

class Command(unittest.TestCase):
    def call(self,source,output):
        return subprocess.run([sys.executable,'-m','raster_objects.cli','--source',str(source),'--output',str(output)],capture_output=True,text=True)
    def test_cli_replay(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'out.json';src=ROOT/'fixtures/input.json';r=self.call(src,out);self.assertEqual(r.returncode,0,r.stderr);first=out.read_bytes();self.assertEqual(json.loads(first)['source_sha256'],hashlib.sha256(src.read_bytes()).hexdigest());self.assertEqual(self.call(src,out).returncode,0);self.assertEqual(first,out.read_bytes())
    def test_failure_preserves(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'bad.json';out=Path(td)/'out.json';out.write_text('prior')
            for body in ['{}','{"a":1,"a":2}','NaN']:
                src.write_text(body);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(out.read_text(),'prior')
    def test_same_path_and_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'in.json';src.write_bytes((ROOT/'fixtures/input.json').read_bytes());before=src.read_bytes();self.assertEqual(self.call(src,src).returncode,2);self.assertEqual(src.read_bytes(),before);out=Path(td)/'link.json';out.symlink_to(src);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(src.read_bytes(),before)
