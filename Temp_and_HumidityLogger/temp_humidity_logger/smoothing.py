"""Chart smoothing helpers."""


def append_and_average(history, timestamp, value, *, is_satellite, sample_window, time_window_s):
    """Append a sample and return the smoothed value for chart plotting."""
    numeric_value = float(value)
    history.append((timestamp, numeric_value))

    if is_satellite:
        window_s = int(time_window_s)
        if window_s <= 0:
            return numeric_value
        cutoff_ts = timestamp.timestamp() - window_s
        while history and history[0][0].timestamp() < cutoff_ts:
            history.popleft()
        values = [item_value for _, item_value in history]
        return sum(values) / float(len(values))

    values = [item_value for _, item_value in list(history)[-int(sample_window):]]
    return sum(values) / float(len(values))
