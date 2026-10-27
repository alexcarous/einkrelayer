# Dependencies

einkrelayer is distributed under AGPL-3.0-only. Dependency packages retain their
own notices and license terms; source and license files are included in their
installed distributions.

| Component | Declared license |
| --- | --- |
| PyMuPDF / MuPDF | AGPL-3.0 (commercial licensing also offered by Artifex) |
| CairoSVG | LGPL-3.0-or-later |
| Beautiful Soup | MIT |
| Pillow | MIT-CMU |
| python-dotenv | BSD-3-Clause |
| tinycss2 | BSD-3-Clause |
| markdown-it-py | MIT |
| ziamath | MIT |
| mammoth | BSD-2-Clause |
| lxml | BSD-3-Clause, with bundled library notices |
| dkimpy | BSD-style |
| dnspython | ISC |

`uv.lock` records resolved versions and transitive dependencies. This table
summarizes runtime components; consult each distribution's license files for the
complete terms, including native libraries. Test fixtures are synthetic. System
fonts, Cairo, Java, and EPUBCheck are installed separately and retain their own
licenses.
