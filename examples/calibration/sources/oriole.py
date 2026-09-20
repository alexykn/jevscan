def choose_route(request, router):
    try:
        return router.route(request)
    except Exception:
        return "default"
