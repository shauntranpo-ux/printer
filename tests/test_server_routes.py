def test_no_new_route():
    """The /new route must not exist — it's a duplicate of /."""
    import server
    rules = [str(r) for r in server.app.url_map.iter_rules()]
    assert "/new" not in rules, f"/new route still exists: {rules}"
