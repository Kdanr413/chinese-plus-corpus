from __future__ import annotations
import argparse
import json
import uuid
from pathlib import Path
from corpus_releases import validate_release
from corpus_registry import RegistryNotFoundError

class ReleaseCatalog:
    """Read-only adapter for immutable filesystem Releases, without a database."""
    def __init__(self, root: Path, *, allowed: set[tuple[str, str]] | None = None):
        self.root = root.resolve()
        self.entries = {}
        self.corpora = {}
        for manifest in sorted(self.root.glob('*/*/manifest.json')):
            release = manifest.parent
            if allowed is not None and (release.parent.name, release.name) not in allowed:
                continue
            if not release.resolve().is_relative_to(self.root):
                raise ValueError('Release path escapes the catalog')
            report = validate_release(release)
            if not report.valid:
                raise ValueError('Release integrity check failed: ' + release.name)
            m = json.loads(manifest.read_text(encoding='utf-8-sig'))
            key, version = m['corpus_id'], m['version']
            if release.parent.name != key or release.name != version:
                raise ValueError('Release identity does not match its directory')
            if not m['index_built']:
                continue
            cid = uuid.uuid5(uuid.NAMESPACE_URL, 'corpus:' + key)
            vid = uuid.uuid5(cid, version)
            date = m['created_at']
            self.corpora.setdefault(key, dict(id=cid, corpus_key=key, name=key,
                description='', status='active', default_version_id=None,
                created_at=date, updated_at=date))
            row = dict(id=vid, corpus_id=cid, version=version, status='published',
                searchable=True, parent_version_id=None,
                release_path=release.relative_to(self.root).as_posix(),
                corpus_format_version=m['corpus_format_version'],
                pipeline_version=m['pipeline_version'], search_engine_version=m['search_engine_version'],
                checksum_sha256=m['checksum']['value'], created_at=date, updated_at=date)
            self.entries[key, version] = (release, m, row)
        for key, corpus in self.corpora.items():
            rows = self.list_versions(corpus['id'])
            corpus['default_version_id'] = max(rows, key=lambda r: (r['created_at'], r['version']))['id']

    def list_corpora(self):
        return list(self.corpora.values())

    def get_corpus(self, key):
        if key not in self.corpora:
            raise RegistryNotFoundError('Corpus not found')
        return self.corpora[key]

    def list_versions(self, corpus_id):
        return [row for _, _, row in self.entries.values() if str(row['corpus_id']) == str(corpus_id)]

    def get_version(self, key, version=None):
        corpus = self.get_corpus(key)
        if version is None:
            return next(row for row in self.list_versions(corpus['id']) if row['id'] == corpus['default_version_id'])
        if (key, version) not in self.entries:
            raise RegistryNotFoundError('Version not found')
        return self.entries[key, version][2]

    def resolve_release_path(self, key, version):
        self.get_version(key, version)
        return self.entries[key, version][0]

    def verify_registered_files(self, key, version):
        return validate_release(self.resolve_release_path(key, version)).as_dict()

    def list_files(self, key, version):
        self.get_version(key, version)
        return [dict(item, encoding='gb18030' if item['role'] == 'corpus' else None)
                for item in self.entries[key, version][1]['files']]


def main():
    p = argparse.ArgumentParser(description='Validate portable corpus Releases')
    p.add_argument('root', type=Path)
    a = p.parse_args()
    catalog = ReleaseCatalog(a.root)
    if not catalog.entries:
        raise SystemExit('No indexed Releases found')
    print(json.dumps({'valid': True, 'releases': len(catalog.entries)}, ensure_ascii=False))

if __name__ == '__main__':
    main()
