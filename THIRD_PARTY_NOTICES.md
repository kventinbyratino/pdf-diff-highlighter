# Third-party notices

This project is licensed under the MIT License. See `LICENSE`.

The application depends on third-party Python packages. The table below is the project license inventory for direct runtime/test dependencies and the known transitive dependency set checked by `scripts/check_licenses.py`.

## Direct dependencies

| Package | Role | License/status | Source |
|---|---|---|---|
| Flask | Web framework | BSD-3-Clause | https://github.com/pallets/flask |
| Gunicorn | Linux WSGI runtime | MIT | https://github.com/benoitc/gunicorn |
| pypdfium2 | PDF rendering wrapper | BSD-3-Clause + Apache-2.0 + bundled third-party notices | https://github.com/pypdfium2-team/pypdfium2 |
| pypdf | PDF metadata/page utility | BSD-3-Clause | https://github.com/py-pdf/pypdf |
| reportlab | Test/document PDF generation support | BSD-3-Clause | https://www.reportlab.com/ |
| Pillow | Image processing | HPND-style permissive license | https://github.com/python-pillow/Pillow |
| opencv-python-headless | Optional alignment/image analysis | Apache-2.0 | https://github.com/opencv/opencv-python |
| pytest | Test runner | MIT | https://github.com/pytest-dev/pytest |

## Transitive packages checked in CI

The license scan currently checks the installed dependency closure from `requirements.txt` and allows only reviewed permissive licenses.

Known transitive packages at the time of writing:

| Package | License/status |
|---|---|
| blinker | MIT |
| charset-normalizer | MIT |
| click | BSD-3-Clause |
| iniconfig | MIT |
| itsdangerous | BSD-3-Clause/BSD License |
| Jinja2 | BSD-3-Clause/BSD License |
| MarkupSafe | BSD-3-Clause |
| numpy | BSD-3-Clause |
| packaging | Apache-2.0 OR BSD-2-Clause |
| pluggy | MIT |
| Pygments | BSD-2-Clause |
| Werkzeug | BSD-3-Clause |

## pypdfium2 / PDFium note

`pypdfium2` wraps PDFium and documents additional bundled/dependency notices upstream. Keep the package-provided notices with release artifacts when distributing binaries or vendor bundles.

## Maintenance rule

When adding or updating dependencies:

1. run `python scripts/check_licenses.py` in the project virtual environment;
2. update this file if a new reviewed dependency appears;
3. do not merge unknown, AGPL, GPL, LGPL, proprietary, or commercial-only license text without explicit product/legal approval.
