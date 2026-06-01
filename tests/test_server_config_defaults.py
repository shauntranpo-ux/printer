import sys

def test_loss_limit_default_is_sane():
    if 'server' in sys.modules:
        del sys.modules['server']
    import server
    default = server._FULL_CONFIG_DEFAULT
    assert default["daily_loss_limit_dollars"] <= 200, (
        f"daily_loss_limit_dollars default is {default['daily_loss_limit_dollars']} — too high"
    )
    assert default["daily_profit_target_dollars"] <= 500, (
        f"daily_profit_target_dollars default is {default['daily_profit_target_dollars']} — too high"
    )

def test_config_sentinel_default_is_sane():
    if 'server' in sys.modules:
        del sys.modules['server']
    import server
    sentinel = server._CONFIG_DEFAULT
    assert sentinel["daily_loss_limit_dollars"] <= 200
    assert sentinel["daily_profit_target_dollars"] <= 500
