from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = PROJECT_ROOT / 'requirements.txt'

# Source of truth for packages whose PyPI metadata is incomplete or ambiguous.
# Keep this list intentionally explicit: a new dependency with an unknown or
# copyleft license should fail CI until it is reviewed and added here.
LICENSE_OVERRIDES = {
    'click': 'BSD-3-Clause',
    'flask': 'BSD-3-Clause',
    'iniconfig': 'MIT',
    'markupsafe': 'BSD-3-Clause',
    'numpy': 'BSD-3-Clause',
    'packaging': 'Apache-2.0 OR BSD-2-Clause',
    'pillow': 'HPND',
    'pytest': 'MIT',
    'pygments': 'BSD-2-Clause',
    'pypdf': 'BSD-3-Clause',
    'pypdfium2': 'BSD-3-Clause AND Apache-2.0 AND third-party notices',
    'reportlab': 'BSD-3-Clause',
    'werkzeug': 'BSD-3-Clause',
}

ALLOWED_LICENSE_TOKENS = {
    'Apache Software License',
    'Apache-2.0',
    'BSD License',
    'BSD-2-Clause',
    'BSD-3-Clause',
    'HPND',
    'MIT',
    'MIT License',
    'Python Software Foundation License',
    'third-party notices',
}

FORBIDDEN_PATTERNS = (
    'AGPL',
    'GPL',
    'LGPL',
    'Affero',
    'Commercial',
    'Proprietary',
)

NAME_RE = re.compile(r'^[A-Za-z0-9_.-]+')


def normalize_name(name: str) -> str:
    return name.replace('_', '-').lower()


def requirement_names() -> set[str]:
    names: set[str] = set()
    for raw in REQUIREMENTS.read_text(encoding='utf-8').splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        match = NAME_RE.match(line)
        if match:
            names.add(normalize_name(match.group(0)))
    return names


def dependency_closure(roots: set[str]) -> set[str]:
    pending = list(roots)
    seen: set[str] = set()
    while pending:
        name = normalize_name(pending.pop())
        if name in seen:
            continue
        seen.add(name)
        try:
            reqs = metadata.requires(name) or []
        except metadata.PackageNotFoundError:
            raise SystemExit(f'Missing installed package: {name}')
        for req_text in reqs:
            try:
                req = Requirement(req_text)
            except Exception:
                continue
            marker = req.marker
            if marker is not None and not marker.evaluate():
                continue
            pending.append(normalize_name(req.name))
    return seen


def license_from_metadata(name: str) -> str:
    normalized = normalize_name(name)
    if normalized in LICENSE_OVERRIDES:
        return LICENSE_OVERRIDES[normalized]
    md = metadata.metadata(name)
    license_field = md['License'] if 'License' in md else ''
    classifiers = md.get_all('Classifier') or []
    license_classifiers = [item.rsplit('::', 1)[-1].strip() for item in classifiers if item.startswith('License ::')]
    candidates = [license_field.strip(), *license_classifiers]
    candidates = [item for item in candidates if item and not item.startswith('Development Status')]
    return '; '.join(candidates)


def is_allowed(license_text: str) -> bool:
    if not license_text:
        return False
    if any(pattern.lower() in license_text.lower() for pattern in FORBIDDEN_PATTERNS):
        # Allow LGPL-like text only after explicit review; currently no override needs it.
        return False
    return any(token.lower() in license_text.lower() for token in ALLOWED_LICENSE_TOKENS)


def main() -> int:
    roots = requirement_names()
    packages = sorted(dependency_closure(roots))
    failures: list[str] = []
    for package in packages:
        version = metadata.version(package)
        license_text = license_from_metadata(package)
        status = 'OK' if is_allowed(license_text) else 'BLOCKED'
        print(f'{status}\t{package}\t{version}\t{license_text or "UNKNOWN"}')
        if status != 'OK':
            failures.append(f'{package}=={version}: {license_text or "UNKNOWN"}')
    if failures:
        print('\nLicense scan failed for:', file=sys.stderr)
        for failure in failures:
            print(f'- {failure}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
