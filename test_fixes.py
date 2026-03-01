"""Quick verification of all 3 fixes."""
import sys

# Test 1: Config relay methods
from config import Config
print('=== Config Tests ===')
print(f'CLOB_RELAY_URL: "{Config.CLOB_RELAY_URL}"')
print(f'get_clob_url(): {Config.get_clob_url()}')
print(f'is_relay_enabled(): {Config.is_relay_enabled()}')
assert Config.get_clob_url() == 'https://clob.polymarket.com', 'Should default to direct URL'
assert Config.is_relay_enabled() == False, 'Should be False when no relay set'
print('  Config relay defaults OK')

# Test 2: Seed mode filter
from trading.live_balance_manager import LiveBalanceManager, LIVE_MODES
bm = LiveBalanceManager(3.0, 'seed')
filt = bm.get_strategy_filter()
print(f'\n=== Seed Mode ===')
print(f'Min confidence: {LIVE_MODES["seed"].min_confidence}')
print(f'Enabled: {filt["enabled"]}')
assert LIVE_MODES['seed'].min_confidence == 0.90
assert 'mean_reversion' not in filt['enabled']
assert 'spike_fade' not in filt['enabled']
assert 'binance_momentum' not in filt['enabled']
assert 'yes_no_arb' in filt['enabled']
assert 'prob_closer' in filt['enabled']
print('  Seed mode: only arbs + prob_closer OK')

# Test 3: Timing guards
from strategies.swing_scalpers import MeanReversionScalper, SpikeFade
print(f'\n=== Timing Guards ===')
print(f'MeanReversion MIN_SECONDS_LEFT: {MeanReversionScalper.MIN_SECONDS_LEFT}')
print(f'SpikeFade MIN_SECONDS_LEFT: {SpikeFade.MIN_SECONDS_LEFT}')
assert MeanReversionScalper.MIN_SECONDS_LEFT == 45
assert SpikeFade.MIN_SECONDS_LEFT == 45
print('  45s minimum for risky strategies OK')

# Test 4: All strategies load
from strategies.swing_scalpers import ExpiryRush, BinanceMomentumSniper
print(f'\n=== All 4 New Strategies ===')
for s in [MeanReversionScalper, SpikeFade, ExpiryRush, BinanceMomentumSniper]:
    inst = s()
    print(f'  {inst.name}: {inst.description}')
print('  All strategies load OK')

# Test 5: Graduation
from trading.live_balance_manager import GRADUATION_THRESHOLDS
print(f'\n=== Graduation ===')
for mode, (next_mode, threshold) in GRADUATION_THRESHOLDS.items():
    print(f'  {mode} -> {next_mode} at ${threshold}')

print(f'\nALL TESTS PASSED')
