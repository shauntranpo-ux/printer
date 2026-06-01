def test_no_unused_heavy_deps():
    """requirements.txt must not include packages unused by live bot."""
    with open('requirements.txt') as f:
        reqs = f.read()
    assert 'httpx' not in reqs, "httpx is unused in live code"
    assert 'pyarrow' not in reqs, "pyarrow is unused"
    assert 'scikit-learn' not in reqs, "scikit-learn is only in dead quarantine"

def test_anthropic_in_requirements():
    with open('requirements.txt') as f:
        reqs = f.read()
    assert 'anthropic' in reqs, "anthropic SDK missing"

def test_aiosqlite_present():
    with open('requirements.txt') as f:
        reqs = f.read()
    assert 'aiosqlite' in reqs, "aiosqlite must stay — used by bot_infra.py"
