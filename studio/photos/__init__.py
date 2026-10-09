"""Where photos come from (v6).

The models are data in `catalogue.yaml`, each provider is one adapter (`cloudflare`, `openai`,
`google`, and the stand-in in `fake`), and every caller asks
`studio.photos.registry.PhotoRegistry` for a model's adapter, key, options, price and limits.
"""
