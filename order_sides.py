"""Normalize SDK/Cap'n Proto enums at the boundary, before keys or arithmetic."""


def side_name(value):
    # Cap'n Proto DynamicEnum supports str() and string equality, but is not a
    # string: e.g. 'reduce_' + value raises TypeError despite value == 'bid'.
    text = str(value)
    if text in ('bid', 'ask'):
        return text
    for attr in ('value', 'name'):
        candidate = getattr(value, attr, None)
        if isinstance(candidate, str) and candidate.lower() in ('bid', 'ask'):
            return candidate.lower()
    raise ValueError(f'unknown order side: {text}')
