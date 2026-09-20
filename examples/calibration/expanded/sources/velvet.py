def read_catalog(path, opener):
    try:
        with opener.open(path) as handle:
            catalog = decode_catalog(handle)
            if not catalog["items"]:
                raise ValueError("empty catalog")
            return catalog
    except OSError:
        return {"items": [], "source": "unavailable"}
