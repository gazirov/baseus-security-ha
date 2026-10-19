# Local integration icon

Original yellow camera pictogram for this unofficial integration. It is not
the official Baseus logo and does not use Home Assistant branded artwork.
Rebuild both PNGs with `python tools/build_ha_brand.py` (Pillow required).

Home Assistant 2026.3+ loads these images from the integration's `brand/`
directory. Domain `baseus_s1` is retained for existing installations. Logos and
dark images intentionally fall back to the same square icon.
