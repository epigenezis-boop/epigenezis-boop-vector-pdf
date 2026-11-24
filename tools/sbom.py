"""Generate a CycloneDX 1.5 JSON SBOM for the VECTOR PDF runtime."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from importlib import metadata
import json
import platform
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vector_pdf.version import VERSION  # noqa: E402


def _normalize(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def _license(component):
    expression = component.get('License-Expression')
    if expression and expression.upper() != 'UNKNOWN':
        return expression
    value = component.get('License')
    if value and len(value) < 200 and value.upper() != 'UNKNOWN':
        return value
    for classifier in component.get_all('Classifier') or []:
        if classifier.startswith('License ::'):
            return classifier.rsplit('::', 1)[-1].strip()
    return None


def _component(distribution):
    name = distribution.metadata.get('Name')
    version = distribution.version
    if not name or not version:
        return None
    result = {
        'type': 'library',
        'name': name,
        'version': version,
        'purl': f'pkg:pypi/{_normalize(name)}@{version}',
    }
    license_name = _license(distribution.metadata)
    if license_name:
        result['licenses'] = [{'license': {'name': license_name}}]
    return result


def generate(version=None, packages=None):
    """Return a CycloneDX 1.5 document for installed distributions."""
    version = version or VERSION
    components = []
    seen = set()
    for distribution in metadata.distributions():
        component = _component(distribution)
        if component is None:
            continue
        key = (component['name'].casefold(), component['version'])
        if key in seen:
            continue
        seen.add(key)
        if packages is not None and component['name'].casefold() not in packages:
            continue
        components.append(component)
    components.sort(key=lambda item: (item['name'].casefold(), item['version']))
    return {
        'bomFormat': 'CycloneDX',
        'specVersion': '1.5',
        'version': 1,
        'metadata': {
            'timestamp': datetime.now(timezone.utc).strftime(
                '%Y-%m-%dT%H:%M:%SZ'),
            'tools': [{
                'vendor': 'VECTOR PDF',
                'name': 'tools/sbom.py',
                'version': '1',
            }],
            'component': {
                'type': 'application',
                'name': 'VECTOR PDF',
                'version': version,
                'licenses': [{'license': {'name': 'MIT'}}],
                'properties': [
                    {'name': 'python.version',
                     'value': platform.python_version()},
                    {'name': 'target.platform', 'value': 'Windows x64'},
                ],
            },
        },
        'components': components,
    }


def write_sbom(path, version=None, packages=None):
    document = generate(version, packages)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(document, indent=2, sort_keys=True) + '\n',
        encoding='utf-8', newline='\n')
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('sbom.json'))
    parser.add_argument('--version', default=None)
    args = parser.parse_args(argv)
    destination = write_sbom(args.output, args.version)
    print(destination)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
