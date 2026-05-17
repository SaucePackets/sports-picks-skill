# Pick DB Schema & Normalization

## Canonical Template

Every MLB pick row in Agent Memory must match this shape:

| Field | Format | Example |
|-------|--------|---------|
| `pick_side` | Team name only, no "ML" suffix | `'Philadelphia Phillies'` |
| `price` | Moneyline number only | `'-170'` |
| `stake` | Unit notation | `'1u'` |
| `verdict` | Team name | `'Philadelphia Phillies'` |
| `confidence` | Controlled value | `'Medium'` |
| `result` | `'pending'`, `'win'`, `'loss'`, `'push'` | `'pending'` |

## `analysis_json` Keys

Use old-convention key names, not the verbose ones:

| Use | Don't Use |
|-----|-----------|
| `polymarket_fill_price` | ~~`polymarket_entry_price`~~ |
| `polymarket_framing_fill_price` | ~~`polymarket_orderbook_price`~~ |
| `polymarket_order_status` | ~~`polymarket_order_state`~~ |
| `normalized_scalar_originals` | *(required)* |
| `price_source` | `'draftkings'` |

## `metadata_json` Keys

```
{
  "metadata_schema_version": "1.0"
}
```

## Normalization Function

```python
import re

def normalize_pick_fields(pick_side_raw: str, price_raw: str, stake_raw: str) -> dict:
    """Returns normalized top-level fields + normalized_scalar_originals."""
    # Extract just the team name (strip "ML" suffix)
    team = re.sub(r'\s+ML$', '', pick_side_raw).strip()
    
    # Extract just the moneyline number
    m = re.search(r'([+-]?\d+)', price_raw)
    price = m.group(1) if m else price_raw
    
    return {
        'top_level': {
            'pick_side': team,
            'price': price,
            'stake': '1u',
            'verdict': team,
        },
        'normalized_scalar_originals': {
            'price': price_raw,
            'stake': stake_raw,
            'pick_side': pick_side_raw,
            'verdict': pick_side_raw,
        }
    }
```

## DB Insert Pattern

When inserting a new pick row directly (Console API is down):

```python
normalized = normalize_pick_fields(pick_side, price, stake)

pick = PickAnalysis(
    pick_side=normalized['top_level']['pick_side'],
    price=normalized['top_level']['price'],
    stake=normalized['top_level']['stake'],
    verdict=normalized['top_level']['verdict'],
    ...
)
a = dict(analysis_json or {})
a['polymarket_fill_price'] = ...      # old naming convention
a['polymarket_framing_fill_price'] = ...
a['polymarket_order_status'] = ...
a['normalized_scalar_originals'] = normalized['normalized_scalar_originals']
a['price_source'] = 'draftkings'
# Remove any duplicate/new-format keys
a.pop('polymarket_entry_price', None)
a.pop('polymarket_order_state', None)
a.pop('polymarket_bb_bid', None)
a.pop('polymarket_bb_ask', None)
a.pop('polymarket_notes', None)

m = dict(metadata_json or {})
m['metadata_schema_version'] = '1.0'
```
