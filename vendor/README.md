# Bundled third-party code

`waveshare_epd/` - the drivers for the Waveshare 2.13" e-Paper HAT: V4
(`epd2in13_V4.py`, the tested one), V3 (`epd2in13_V3.py`) and V2
(`epd2in13_V2.py`), plus `epdconfig.py` (GPIO/SPI, shared by all three),
copied unchanged from Waveshare's e-Paper repository
(https://github.com/waveshareteam/e-Paper,
`RaspberryPi_JetsonNano/python/lib/waveshare_epd/`; V2 and V3 fetched
2026-10-24). MIT licence: see the header at the top of each file. Bundled
so a new dock doesn't need the whole Waveshare repository installed.
`[display] model` in config.toml picks the driver (dock_status.Panel).
