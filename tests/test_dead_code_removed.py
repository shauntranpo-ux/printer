def test_all_markets_cache_ts_removed():
    """_all_markets_cache_ts must not exist — set but never read."""
    import bot_state
    assert not hasattr(bot_state, '_all_markets_cache_ts'), (
        "_all_markets_cache_ts still exists in bot_state"
    )


def test_brain_column_in_create_table():
    """CREATE TABLE in bot_infra must include brain column."""
    with open('bot_infra.py', encoding='utf-8') as f:
        src = f.read()
    create_start = src.index('CREATE TABLE IF NOT EXISTS trades')
    # Find the closing of the CREATE TABLE block
    create_block = src[create_start:create_start + 2000]
    # brain must appear before the closing paren of CREATE TABLE
    closing_paren_pos = create_block.index('\n            )\n')
    brain_pos = create_block.find('brain')
    assert brain_pos != -1 and brain_pos < closing_paren_pos, \
        "brain column missing from CREATE TABLE block"
