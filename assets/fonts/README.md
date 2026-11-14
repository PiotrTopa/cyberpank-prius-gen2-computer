# Fonts

Loaded by `cyberpunk_computer/ui/fonts.py` (file names lowercase, no spaces).

| File | Role in `fonts.py` | Use |
|------|---------|-------|
| `orbitron.ttf` | `title`, `header`, `display` | headers and titles; acceptable without anti-aliasing from 10 px |
| `terminus.ttf` | `mono`, `standard` | UI text; clean without anti-aliasing only at 14 px and even sizes above |
| `04b03.ttf` | `tiny`, `pixel` | pixel font, exact at every size; used for text below 8 px and for mono text below 14 px |
| `fontawesome.otf` | `icons`, `icon` | Font Awesome 7 Free Solid icons |

All fonts render 1-bit (`ANTIALIAS = False`): the MFD is fed over an analog RGBS link and
anti-aliased edges show as colour fringing. A missing file falls back to pygame's default font.

Sources and licences: Orbitron (Google Fonts, OFL), Terminus (Terminus Font Project, OFL),
04b03 (04 font family, freeware), Font Awesome Free (OFL / CC BY 4.0).
