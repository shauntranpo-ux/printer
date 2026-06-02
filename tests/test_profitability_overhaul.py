def test_dashboard_badge_uses_brain_field():
    """Dashboard trades badge must use brain field when present."""
    with open('handoff/Money Printer.html', encoding='utf-8') as f:
        src = f.read()
    # mapTrade must map the brain field
    assert "brain:" in src or "t.brain" in src, \
        "mapTrade does not map the brain field from the API response"
    # Badge must prefer brain over strategy_variant
    assert "t.brain==='s1'" in src or "brain==='s1'" in src, \
        "Badge does not use brain field for S1/S2 label"
