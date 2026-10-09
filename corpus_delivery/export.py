import argparse
import json
import shutil
from pathlib import Path
from corpus_releases import validate_release

def export_release(source, destination):
    if not validate_release(source).valid:
        raise ValueError('Source Release failed integrity validation')
    m = json.loads((source / 'manifest.json').read_text(encoding='utf-8-sig'))
    target = destination / m['corpus_id'] / m['version']
    for field in ('corpus_id','version'):
        value = str(m[field])
        if value in ('', '.', '..') or any(char in value for char in ('/', chr(92))):
            raise ValueError('Unsafe Release identity')
    if not target.resolve().is_relative_to(destination.resolve()):
        raise ValueError('Destination escapes the requested root')
    if target.exists():
        raise FileExistsError('Destination exists; never overwrite a Release')
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    report = validate_release(target)
    if not report.valid:
        raise ValueError('Export failed integrity validation')
    return dict(corpus_id=m['corpus_id'], version=m['version'], files=report.checked_files, checksum_sha256=m['checksum']['value'])

def main():
    p = argparse.ArgumentParser(description='Copy and verify an indexed Corpus Release')
    p.add_argument('source', type=Path)
    p.add_argument('destination', type=Path)
    a = p.parse_args()
    print(json.dumps(export_release(a.source, a.destination), ensure_ascii=False))

if __name__ == '__main__': main()
