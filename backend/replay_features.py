"""Exact .osr parser and replay tensor builders used during training.

Ported directly from the supplied Kaggle training script so serving uses the
same event sequence, high-activity windows, and 53-feature replay summary.
"""

from __future__ import annotations

import io
import lzma
import math
import struct
from pathlib import Path
from typing import BinaryIO

import numpy as np

try:
    import rosu_pp_py as rosu
except ImportError:  # The legacy model remains deployable without the optional calculator.
    rosu = None

EVENT_SEQUENCE_LENGTH = 1024

ACTION_WINDOW_COUNT = 6

ACTION_WINDOW_SECONDS = 1.5

ACTION_WINDOW_HZ = 64

ACTION_WINDOW_LENGTH = int(round(ACTION_WINDOW_SECONDS * ACTION_WINDOW_HZ))

ACTION_WINDOW_MIN_SEPARATION_SECONDS = 1.25

def read_exact(handle, number_of_bytes: int) -> bytes:
    data = handle.read(number_of_bytes)
    if len(data) != number_of_bytes:
        raise EOFError(f'Expected {number_of_bytes} bytes; received {len(data)}.')
    return data

def read_uleb128(handle) -> int:
    result = 0
    shift = 0
    while True:
        byte = read_exact(handle, 1)[0]
        result |= (byte & 127) << shift
        if byte & 128 == 0:
            return result
        shift += 7
        if shift > 63:
            raise ValueError('Malformed ULEB128 value.')

def read_osu_string(handle) -> str:
    marker = read_exact(handle, 1)[0]
    if marker == 0:
        return ''
    if marker != 11:
        raise ValueError(f'Unexpected osu! string marker: {marker:#x}')
    length = read_uleb128(handle)
    return read_exact(handle, length).decode('utf-8', errors='replace')

def parse_osr(source) -> dict:
    """
    Parse a replay path or bytes-like object.

    Event matrix columns:

        cumulative_time_seconds, x, y, key_mask
    """
    if isinstance(source, (str, Path)):
        context = open(source, 'rb')
    elif isinstance(source, (bytes, bytearray, memoryview)):
        context = io.BytesIO(bytes(source))
    else:
        raise TypeError(f'Unsupported replay source: {type(source)}')
    with context as handle:
        mode = struct.unpack('<B', read_exact(handle, 1))[0]
        game_version = struct.unpack('<i', read_exact(handle, 4))[0]
        beatmap_hash = read_osu_string(handle)
        player_name = read_osu_string(handle)
        replay_hash = read_osu_string(handle)
        count_300 = struct.unpack('<H', read_exact(handle, 2))[0]
        count_100 = struct.unpack('<H', read_exact(handle, 2))[0]
        count_50 = struct.unpack('<H', read_exact(handle, 2))[0]
        count_geki = struct.unpack('<H', read_exact(handle, 2))[0]
        count_katu = struct.unpack('<H', read_exact(handle, 2))[0]
        count_miss = struct.unpack('<H', read_exact(handle, 2))[0]
        score = struct.unpack('<i', read_exact(handle, 4))[0]
        max_combo = struct.unpack('<H', read_exact(handle, 2))[0]
        perfect = struct.unpack('<B', read_exact(handle, 1))[0]
        mods_mask = struct.unpack('<i', read_exact(handle, 4))[0]
        life_bar_graph = read_osu_string(handle)
        timestamp = struct.unpack('<q', read_exact(handle, 8))[0]
        compressed_length = struct.unpack('<i', read_exact(handle, 4))[0]
        if compressed_length < 0:
            raise ValueError(f'Negative replay payload length: {compressed_length}')
        compressed_payload = read_exact(handle, compressed_length)
    if compressed_length == 0:
        decoded = ''
    else:
        decoded = lzma.decompress(compressed_payload).decode('utf-8', errors='ignore')
    cumulative_time_ms = 0
    parsed_events = []
    for raw_event in decoded.split(','):
        if not raw_event:
            continue
        fields = raw_event.split('|')
        if len(fields) < 4:
            continue
        try:
            delta_ms = int(float(fields[0]))
            x = float(fields[1])
            y = float(fields[2])
            key_mask = int(float(fields[3]))
        except (ValueError, OverflowError):
            continue
        # Stable/lazer replays may contain negative-delta marker or RNG-seed
        # records before or after the actual cursor frames. They are metadata,
        # not movement. Skipping them (rather than stopping at the first one)
        # keeps the real frames that follow.
        if delta_ms < 0:
            continue
        cumulative_time_ms += delta_ms
        if not (np.isfinite(x) and np.isfinite(y)):
            continue
        parsed_events.append((cumulative_time_ms / 1000.0, x, y, key_mask))
    events = np.asarray(parsed_events, dtype=np.float64)
    if events.size == 0:
        events = np.zeros((0, 4), dtype=np.float64)
    return {'header': {'mode': int(mode), 'game_version': int(game_version), 'beatmap_hash': beatmap_hash, 'player_name': player_name, 'replay_hash': replay_hash, 'count_300': int(count_300), 'count_100': int(count_100), 'count_50': int(count_50), 'count_geki': int(count_geki), 'count_katu': int(count_katu), 'count_miss': int(count_miss), 'score': int(score), 'max_combo': int(max_combo), 'perfect': int(perfect), 'mods_mask': int(mods_mask), 'life_bar_graph': life_bar_graph, 'timestamp': int(timestamp)}, 'events': events}

def collapse_duplicate_timestamps(events: np.ndarray) -> np.ndarray:
    if len(events) <= 1:
        return events
    times = events[:, 0]
    _, reversed_indices = np.unique(times[::-1], return_index=True)
    final_indices = len(times) - 1 - reversed_indices
    final_indices = np.sort(final_indices)
    return events[final_indices]

def key_states_from_masks(key_masks: np.ndarray):
    key_masks = np.asarray(key_masks, dtype=np.int64)
    left = (key_masks & 1 != 0) | (key_masks & 4 != 0)
    right = (key_masks & 2 != 0) | (key_masks & 8 != 0)
    return (left.astype(np.float64), right.astype(np.float64))

def safe_mean(values: np.ndarray) -> float:
    values = np.asarray(values, dtype=np.float64)
    values = values[np.isfinite(values)]
    if len(values) == 0:
        return 0.0
    return float(values.mean())

def safe_std(values: np.ndarray) -> float:
    values = np.asarray(values, dtype=np.float64)
    values = values[np.isfinite(values)]
    if len(values) == 0:
        return 0.0
    return float(values.std())

def safe_quantile(values: np.ndarray, quantile: float) -> float:
    values = np.asarray(values, dtype=np.float64)
    values = values[np.isfinite(values)]
    if len(values) == 0:
        return 0.0
    return float(np.quantile(values, quantile))

def contiguous_true_durations(times: np.ndarray, state: np.ndarray, end_time: float) -> np.ndarray:
    times = np.asarray(times, dtype=np.float64)
    state = np.asarray(state, dtype=bool)
    if len(state) == 0:
        return np.zeros(0, dtype=np.float64)
    transitions = np.diff(state.astype(np.int8), prepend=0)
    starts = times[transitions == 1].tolist()
    ends = times[transitions == -1].tolist()
    if state[-1]:
        ends.append(end_time)
    pair_count = min(len(starts), len(ends))
    return np.asarray([max(0.0, ends[index] - starts[index]) for index in range(pair_count)], dtype=np.float64)

EVENT_CHANNEL_NAMES = ['log_delta_time', 'delta_x', 'delta_y', 'velocity_x', 'velocity_y', 'speed', 'acceleration', 'jerk', 'heading_sin', 'heading_cos', 'turn_rate', 'left_down', 'right_down', 'press', 'release', 'alternation', 'time_progress', 'event_progress']

def build_event_sequence(events: np.ndarray) -> np.ndarray:
    """
    Represent the complete replay by sampling uniformly over
    replay event index.

    This preserves high-frequency event-to-event mechanics much
    better than compressing the whole replay into 512 time bins.
    """
    events = collapse_duplicate_timestamps(events)
    if len(events) < 2:
        return np.zeros((len(EVENT_CHANNEL_NAMES), EVENT_SEQUENCE_LENGTH), dtype=np.float32)
    times = events[:, 0].astype(np.float64)
    times = times - times[0]
    x = np.clip(events[:, 1], -64.0, 576.0)
    y = np.clip(events[:, 2], -64.0, 448.0)
    key_masks = events[:, 3].astype(np.int64)
    duration = max(float(times[-1]), 0.001)
    delta_time = np.diff(times, prepend=times[0])
    positive_delta_times = delta_time[delta_time > 1e-05]
    fallback_delta_time = float(np.median(positive_delta_times)) if len(positive_delta_times) else 1.0 / 60.0
    delta_time[0] = fallback_delta_time
    delta_time = np.clip(delta_time, 0.0001, 2.0)
    delta_x = np.diff(x, prepend=x[0])
    delta_y = np.diff(y, prepend=y[0])
    velocity_x = delta_x / delta_time
    velocity_y = delta_y / delta_time
    speed = np.hypot(velocity_x, velocity_y)
    acceleration = np.diff(speed, prepend=speed[0]) / delta_time
    jerk = np.diff(acceleration, prepend=acceleration[0]) / delta_time
    heading = np.arctan2(velocity_y, velocity_x)
    unwrapped_heading = np.unwrap(heading)
    turn_rate = np.diff(unwrapped_heading, prepend=unwrapped_heading[0]) / delta_time
    left_down, right_down = key_states_from_masks(key_masks)
    any_down = np.maximum(left_down, right_down)
    press = (np.diff(any_down.astype(np.int8), prepend=0) == 1).astype(np.float64)
    release = (np.diff(any_down.astype(np.int8), prepend=0) == -1).astype(np.float64)
    left_press = np.diff(left_down.astype(np.int8), prepend=0) == 1
    right_press = np.diff(right_down.astype(np.int8), prepend=0) == 1
    press_side = np.full(len(events), -1, dtype=np.int8)
    press_side[left_press] = 0
    press_side[right_press] = 1
    alternation = np.zeros(len(events), dtype=np.float64)
    previous_side = -1
    for index in range(len(events)):
        current_side = int(press_side[index])
        if current_side < 0:
            continue
        if previous_side >= 0 and current_side != previous_side:
            alternation[index] = 1.0
        previous_side = current_side
    time_progress = times / duration
    event_progress = np.linspace(0.0, 1.0, len(events), dtype=np.float64)
    raw_features = np.stack([np.log1p(delta_time * 1000.0), np.tanh(delta_x / 128.0), np.tanh(delta_y / 96.0), np.tanh(velocity_x / 2000.0), np.tanh(velocity_y / 2000.0), np.log1p(np.clip(speed, 0.0, 30000.0)) / np.log1p(5000.0), np.sign(acceleration) * (np.log1p(np.clip(np.abs(acceleration), 0.0, 2000000.0)) / np.log1p(100000.0)), np.sign(jerk) * (np.log1p(np.clip(np.abs(jerk), 0.0, 100000000.0)) / np.log1p(5000000.0)), np.sin(heading), np.cos(heading), np.tanh(turn_rate / 30.0), left_down, right_down, press, release, alternation, time_progress, event_progress], axis=0)
    sampled_indices = np.rint(np.linspace(0, len(events) - 1, EVENT_SEQUENCE_LENGTH)).astype(np.int64)
    sequence = raw_features[:, sampled_indices]
    return np.nan_to_num(sequence, nan=0.0, posinf=8.0, neginf=-8.0).astype(np.float32)

WINDOW_CHANNEL_NAMES = ['cursor_x', 'cursor_y', 'velocity_x', 'velocity_y', 'speed', 'acceleration', 'jerk', 'heading_sin', 'heading_cos', 'turn_rate', 'left_down', 'right_down', 'press', 'release', 'alternation', 'global_progress', 'local_progress', 'activity']

def build_dense_replay_grid(events: np.ndarray):
    events = collapse_duplicate_timestamps(events)
    if len(events) < 2:
        return None
    times = events[:, 0].astype(np.float64)
    times = times - times[0]
    duration = max(float(times[-1]), ACTION_WINDOW_SECONDS)
    x_events = np.clip(events[:, 1], -64.0, 576.0)
    y_events = np.clip(events[:, 2], -64.0, 448.0)
    key_masks = events[:, 3].astype(np.int64)
    grid_length = max(ACTION_WINDOW_LENGTH + 1, int(math.ceil(duration * ACTION_WINDOW_HZ)) + 1)
    grid_times = np.arange(grid_length, dtype=np.float64) / ACTION_WINDOW_HZ
    grid_times = np.minimum(grid_times, duration)
    x = np.interp(grid_times, times, x_events)
    y = np.interp(grid_times, times, y_events)
    preceding_indices = np.searchsorted(times, grid_times, side='right') - 1
    preceding_indices = np.clip(preceding_indices, 0, len(times) - 1)
    grid_masks = key_masks[preceding_indices]
    left_down, right_down = key_states_from_masks(grid_masks)
    any_down = np.maximum(left_down, right_down)
    dt = 1.0 / ACTION_WINDOW_HZ
    velocity_x = np.gradient(x, dt)
    velocity_y = np.gradient(y, dt)
    speed = np.hypot(velocity_x, velocity_y)
    acceleration_x = np.gradient(velocity_x, dt)
    acceleration_y = np.gradient(velocity_y, dt)
    acceleration = np.hypot(acceleration_x, acceleration_y)
    jerk = np.abs(np.gradient(acceleration, dt))
    heading = np.arctan2(velocity_y, velocity_x)
    turn_rate = np.gradient(np.unwrap(heading), dt)
    press = (np.diff(any_down.astype(np.int8), prepend=0) == 1).astype(np.float64)
    release = (np.diff(any_down.astype(np.int8), prepend=0) == -1).astype(np.float64)
    left_press = np.diff(left_down.astype(np.int8), prepend=0) == 1
    right_press = np.diff(right_down.astype(np.int8), prepend=0) == 1
    alternation = np.zeros(grid_length, dtype=np.float64)
    previous_side = -1
    for index in range(grid_length):
        current_side = -1
        if left_press[index]:
            current_side = 0
        if right_press[index]:
            current_side = 1
        if current_side < 0:
            continue
        if previous_side >= 0 and current_side != previous_side:
            alternation[index] = 1.0
        previous_side = current_side
    speed_feature = np.log1p(np.clip(speed, 0.0, 30000.0)) / np.log1p(5000.0)
    acceleration_feature = np.log1p(np.clip(acceleration, 0.0, 2000000.0)) / np.log1p(100000.0)
    jerk_feature = np.log1p(np.clip(jerk, 0.0, 100000000.0)) / np.log1p(5000000.0)
    activity = 1.0 * speed_feature + 0.35 * acceleration_feature + 1.75 * press + 0.4 * alternation + 0.15 * np.abs(np.tanh(turn_rate / 30.0))
    global_progress = grid_times / max(duration, 1e-06)
    dense_features = np.stack([np.clip(x / 512.0, -0.125, 1.125), np.clip(y / 384.0, -0.167, 1.167), np.tanh(velocity_x / 2000.0), np.tanh(velocity_y / 2000.0), speed_feature, acceleration_feature, jerk_feature, np.sin(heading), np.cos(heading), np.tanh(turn_rate / 30.0), left_down, right_down, press, release, alternation, global_progress, np.zeros(grid_length, dtype=np.float64), activity], axis=0)
    return {'features': dense_features, 'activity': activity, 'duration': duration}

def select_action_window_centers(activity: np.ndarray) -> list:
    window_length = ACTION_WINDOW_LENGTH
    half_window = window_length // 2
    smoothing_kernel = np.ones(window_length, dtype=np.float64) / window_length
    smoothed_activity = np.convolve(activity, smoothing_kernel, mode='same')
    valid = np.zeros(len(activity), dtype=bool)
    if len(activity) >= window_length:
        valid[half_window:len(activity) - (window_length - half_window) + 1] = True
    scores = np.where(valid, smoothed_activity, -np.inf)
    candidate_indices = np.argsort(scores)[::-1]
    minimum_separation = int(round(ACTION_WINDOW_MIN_SEPARATION_SECONDS * ACTION_WINDOW_HZ))
    centers = []
    for candidate in candidate_indices:
        if not np.isfinite(scores[candidate]):
            continue
        separated = all((abs(int(candidate) - existing) >= minimum_separation for existing in centers))
        if separated:
            centers.append(int(candidate))
        if len(centers) >= ACTION_WINDOW_COUNT:
            break
    if len(centers) < ACTION_WINDOW_COUNT:
        fallback_centers = np.linspace(half_window, max(half_window, len(activity) - (window_length - half_window) - 1), ACTION_WINDOW_COUNT)
        for fallback_center in fallback_centers:
            if len(centers) >= ACTION_WINDOW_COUNT:
                break
            fallback_center = int(round(fallback_center))
            separated = all((abs(fallback_center - existing) >= max(1, minimum_separation // 2) for existing in centers))
            if separated:
                centers.append(fallback_center)
    while len(centers) < ACTION_WINDOW_COUNT:
        centers.append(centers[-1] if centers else half_window)
    return sorted(centers[:ACTION_WINDOW_COUNT])

def build_action_windows(events: np.ndarray) -> np.ndarray:
    dense = build_dense_replay_grid(events)
    if dense is None:
        return np.zeros((ACTION_WINDOW_COUNT, len(WINDOW_CHANNEL_NAMES), ACTION_WINDOW_LENGTH), dtype=np.float32)
    dense_features = dense['features']
    activity = dense['activity']
    centers = select_action_window_centers(activity)
    half_window = ACTION_WINDOW_LENGTH // 2
    windows = []
    for center in centers:
        start = center - half_window
        end = start + ACTION_WINDOW_LENGTH
        left_padding = max(0, -start)
        right_padding = max(0, end - dense_features.shape[1])
        clipped_start = max(0, start)
        clipped_end = min(dense_features.shape[1], end)
        window = dense_features[:, clipped_start:clipped_end]
        if left_padding > 0 or right_padding > 0:
            window = np.pad(window, ((0, 0), (left_padding, right_padding)), mode='edge')
        if window.shape[1] != ACTION_WINDOW_LENGTH:
            raise RuntimeError(f'Incorrect action-window length: {window.shape}')
        window[16] = np.linspace(0.0, 1.0, ACTION_WINDOW_LENGTH, dtype=np.float64)
        windows.append(window)
    return np.nan_to_num(np.stack(windows, axis=0), nan=0.0, posinf=8.0, neginf=-8.0).astype(np.float32)

REPLAY_SUMMARY_NAMES = ['replay_valid', 'duration_seconds', 'event_count_log', 'events_per_second', 'total_distance_log', 'distance_per_second_log', 'path_efficiency', 'speed_mean_log', 'speed_std_log', 'speed_q50_log', 'speed_q90_log', 'speed_q99_log', 'speed_max_log', 'acceleration_mean_log', 'acceleration_q90_log', 'acceleration_q99_log', 'jerk_mean_log', 'jerk_q90_log', 'jerk_q99_log', 'absolute_turn_mean', 'absolute_turn_q90', 'left_down_fraction', 'right_down_fraction', 'both_down_fraction', 'any_down_fraction', 'left_press_count_log', 'right_press_count_log', 'total_press_count_log', 'presses_per_second', 'alternation_fraction', 'press_interval_mean', 'press_interval_std', 'press_interval_q10', 'press_interval_q50', 'press_interval_q90', 'hold_duration_mean', 'hold_duration_std', 'hold_duration_q50', 'hold_duration_q90', 'cursor_x_std', 'cursor_y_std', 'cursor_coverage_fraction', 'count_300_fraction', 'count_100_fraction', 'count_50_fraction', 'miss_fraction', 'score_log', 'max_combo_log', 'combo_to_hits_ratio', 'total_hits_log', 'perfect', 'mode', 'game_version_scaled']

def build_replay_summary(parsed_replay: dict) -> np.ndarray:
    header = parsed_replay['header']
    events = collapse_duplicate_timestamps(parsed_replay['events'])
    if len(events) < 2:
        summary = np.zeros(len(REPLAY_SUMMARY_NAMES), dtype=np.float32)
        summary[REPLAY_SUMMARY_NAMES.index('mode')] = float(header['mode'])
        return summary
    times = events[:, 0].astype(np.float64)
    times = times - times[0]
    duration = max(float(times[-1]), 0.001)
    x = events[:, 1].astype(np.float64)
    y = events[:, 2].astype(np.float64)
    key_masks = events[:, 3].astype(np.int64)
    delta_time = np.diff(times, prepend=times[0])
    positive_delta_time = delta_time[delta_time > 1e-05]
    delta_time[0] = float(np.median(positive_delta_time)) if len(positive_delta_time) else 1.0 / 60.0
    delta_time = np.clip(delta_time, 0.0001, 2.0)
    delta_x = np.diff(x, prepend=x[0])
    delta_y = np.diff(y, prepend=y[0])
    velocity_x = delta_x / delta_time
    velocity_y = delta_y / delta_time
    speed = np.hypot(velocity_x, velocity_y)
    acceleration = np.diff(speed, prepend=speed[0]) / delta_time
    jerk = np.diff(acceleration, prepend=acceleration[0]) / delta_time
    heading = np.unwrap(np.arctan2(velocity_y, velocity_x))
    turn_rate = np.diff(heading, prepend=heading[0]) / delta_time
    left_down, right_down = key_states_from_masks(key_masks)
    any_down = np.maximum(left_down, right_down)
    both_down = left_down * right_down
    left_press_mask = np.diff(left_down.astype(np.int8), prepend=0) == 1
    right_press_mask = np.diff(right_down.astype(np.int8), prepend=0) == 1
    press_events = []
    for timestamp in times[left_press_mask]:
        press_events.append((float(timestamp), 0))
    for timestamp in times[right_press_mask]:
        press_events.append((float(timestamp), 1))
    press_events.sort(key=lambda item: item[0])
    press_times = np.asarray([item[0] for item in press_events], dtype=np.float64)
    if len(press_times) >= 2:
        press_intervals = np.diff(press_times)
    else:
        press_intervals = np.zeros(0, dtype=np.float64)
    alternating_count = 0
    for index in range(1, len(press_events)):
        if press_events[index][1] != press_events[index - 1][1]:
            alternating_count += 1
    alternation_fraction = alternating_count / max(len(press_events) - 1, 1)
    hold_durations = contiguous_true_durations(times=times, state=any_down > 0.5, end_time=duration)
    point_distances = np.hypot(np.diff(x), np.diff(y))
    total_distance = float(point_distances.sum())
    direct_distance = float(math.hypot(x[-1] - x[0], y[-1] - y[0]))
    path_efficiency = direct_distance / max(total_distance, 1e-06)
    coverage_fraction = float(np.max(x) - np.min(x)) * float(np.max(y) - np.min(y)) / (512.0 * 384.0)
    total_hits = header['count_300'] + header['count_100'] + header['count_50'] + header['count_miss']
    safe_total_hits = max(total_hits, 1)
    summary_dictionary = {'replay_valid': 1.0, 'duration_seconds': duration, 'event_count_log': math.log1p(len(events)), 'events_per_second': len(events) / duration, 'total_distance_log': math.log1p(total_distance), 'distance_per_second_log': math.log1p(total_distance / duration), 'path_efficiency': path_efficiency, 'speed_mean_log': math.log1p(max(safe_mean(speed), 0.0)), 'speed_std_log': math.log1p(max(safe_std(speed), 0.0)), 'speed_q50_log': math.log1p(max(safe_quantile(speed, 0.5), 0.0)), 'speed_q90_log': math.log1p(max(safe_quantile(speed, 0.9), 0.0)), 'speed_q99_log': math.log1p(max(safe_quantile(speed, 0.99), 0.0)), 'speed_max_log': math.log1p(max(float(np.max(speed)), 0.0)), 'acceleration_mean_log': math.log1p(max(safe_mean(np.abs(acceleration)), 0.0)), 'acceleration_q90_log': math.log1p(max(safe_quantile(np.abs(acceleration), 0.9), 0.0)), 'acceleration_q99_log': math.log1p(max(safe_quantile(np.abs(acceleration), 0.99), 0.0)), 'jerk_mean_log': math.log1p(max(safe_mean(np.abs(jerk)), 0.0)), 'jerk_q90_log': math.log1p(max(safe_quantile(np.abs(jerk), 0.9), 0.0)), 'jerk_q99_log': math.log1p(max(safe_quantile(np.abs(jerk), 0.99), 0.0)), 'absolute_turn_mean': safe_mean(np.abs(turn_rate)), 'absolute_turn_q90': safe_quantile(np.abs(turn_rate), 0.9), 'left_down_fraction': float(left_down.mean()), 'right_down_fraction': float(right_down.mean()), 'both_down_fraction': float(both_down.mean()), 'any_down_fraction': float(any_down.mean()), 'left_press_count_log': math.log1p(int(left_press_mask.sum())), 'right_press_count_log': math.log1p(int(right_press_mask.sum())), 'total_press_count_log': math.log1p(len(press_events)), 'presses_per_second': len(press_events) / duration, 'alternation_fraction': alternation_fraction, 'press_interval_mean': safe_mean(press_intervals), 'press_interval_std': safe_std(press_intervals), 'press_interval_q10': safe_quantile(press_intervals, 0.1), 'press_interval_q50': safe_quantile(press_intervals, 0.5), 'press_interval_q90': safe_quantile(press_intervals, 0.9), 'hold_duration_mean': safe_mean(hold_durations), 'hold_duration_std': safe_std(hold_durations), 'hold_duration_q50': safe_quantile(hold_durations, 0.5), 'hold_duration_q90': safe_quantile(hold_durations, 0.9), 'cursor_x_std': float(np.std(x)), 'cursor_y_std': float(np.std(y)), 'cursor_coverage_fraction': coverage_fraction, 'count_300_fraction': header['count_300'] / safe_total_hits, 'count_100_fraction': header['count_100'] / safe_total_hits, 'count_50_fraction': header['count_50'] / safe_total_hits, 'miss_fraction': header['count_miss'] / safe_total_hits, 'score_log': math.log1p(max(header['score'], 0)), 'max_combo_log': math.log1p(max(header['max_combo'], 0)), 'combo_to_hits_ratio': header['max_combo'] / safe_total_hits, 'total_hits_log': math.log1p(total_hits), 'perfect': float(header['perfect']), 'mode': float(header['mode']), 'game_version_scaled': header['game_version'] / 100000000.0}
    summary = np.asarray([summary_dictionary[name] for name in REPLAY_SUMMARY_NAMES], dtype=np.float32)
    return np.nan_to_num(summary, nan=0.0, posinf=1000000.0, neginf=-1000000.0).astype(np.float32)

def calculate_local_score_pp(
    beatmap_content: str | bytes | None,
    header: dict,
    mods: list[str] | tuple[str, ...] | set[str],
) -> float | None:
    """Calculate stable osu!standard PP directly from a map and replay header.

    This is preferable to profile PP (which leaks rank) and more complete than
    public-score matching because it also works for a replay that is no longer
    exposed as a public score.  Public API PP remains a validation/fallback.
    """
    if rosu is None or not beatmap_content:
        return None
    try:
        content = (
            beatmap_content.decode("utf-8", errors="replace")
            if isinstance(beatmap_content, bytes)
            else str(beatmap_content)
        )
        beatmap = rosu.Beatmap(content=content)
        if beatmap.is_suspicious():
            return None
        normalized_mods = [str(token).upper() for token in mods]
        if "NC" in normalized_mods:
            normalized_mods = [token for token in normalized_mods if token != "DT"]
        if "PF" in normalized_mods:
            normalized_mods = [token for token in normalized_mods if token != "SD"]
        performance = rosu.Performance(
            mods=normalized_mods,
            lazer=False,
            combo=max(0, int(header.get("max_combo", 0) or 0)),
            n300=max(0, int(header.get("count_300", 0) or 0)),
            n100=max(0, int(header.get("count_100", 0) or 0)),
            n50=max(0, int(header.get("count_50", 0) or 0)),
            misses=max(0, int(header.get("count_miss", 0) or 0)),
            legacy_total_score=max(0, int(header.get("score", 0) or 0)),
        )
        attributes = performance.calculate(beatmap)
        value = float(attributes.pp)
        return value if math.isfinite(value) and value >= 0.0 else None
    except Exception:
        return None


# ============================================================
# V2 map-aware / score-aware static features
# ============================================================

# The legacy 19 + 53 features remain unchanged above so an existing model.onnx
# continues to work.  The v2 trainer and serving bundle use this additional,
# deterministic feature vector.  Everything here is derived from the replay,
# beatmap, and the play's own public score metadata.  No player identity or
# profile-total pp is included.

MODEL_MOD_TOKENS_V2 = [
    "NF", "EZ", "HD", "HR", "SD", "DT", "HT", "NC", "FL", "SO", "PF"
]

MAP_AWARE_FEATURE_NAMES = [
    "beatmap_available",
    "map_ar_base",
    "map_od_base",
    "map_cs_base",
    "map_hp_base",
    "map_ar",
    "map_od",
    "map_cs",
    "map_hp",
    "map_ar_preempt_ms",
    "map_od_hit_window_300_ms",
    "map_circle_radius",
    "map_bpm_log",
    "map_slider_multiplier",
    "map_object_count_log",
    "map_circle_fraction",
    "map_slider_fraction",
    "map_spinner_fraction",
    "map_objects_per_second",
    "map_interval_mean",
    "map_interval_std",
    "map_interval_q10",
    "map_interval_q50",
    "map_interval_q90",
    "map_stream_fraction_125ms",
    "map_stream_fraction_100ms",
    "map_stream_fraction_80ms",
    "map_jump_mean_log",
    "map_jump_std_log",
    "map_jump_q50_log",
    "map_jump_q90_log",
    "map_jump_q99_log",
    "map_angle_mean",
    "map_angle_std",
    "map_angle_q10",
    "map_angle_q50",
    "map_angle_q90",
    "map_density_star_interaction",
    "press_object_ratio",
    "timing_match_fraction",
    "timing_offset_ms",
    "timing_residual_mean_ms",
    "timing_residual_abs_mean_ms",
    "timing_residual_std_ms",
    "timing_residual_q50_abs_ms",
    "timing_residual_q90_abs_ms",
    "timing_residual_q99_abs_ms",
    "timing_ur_proxy_log",
    "timing_early_fraction",
    "timing_late_fraction",
    "aim_match_fraction",
    "aim_error_mean_log",
    "aim_error_q50_log",
    "aim_error_q90_log",
    "aim_error_q99_log",
    "aim_error_radius_mean",
    "aim_error_radius_q90",
    "aim_inside_circle_fraction",
    "aim_bias_x",
    "aim_bias_y",
    "cursor_speed_at_object_q50_log",
    "cursor_speed_at_object_q90_log",
    "cursor_speed_at_object_q99_log",
    "rhythm_interval_log_ratio_mean",
    "rhythm_interval_log_ratio_std",
    "rhythm_interval_corr",
    "object_cursor_path_ratio_log",
]

SCORE_CONTEXT_FEATURE_NAMES = [
    "score_pp_available",
    "score_pp_log",
    "score_pp_per_star",
    "score_pp_per_hit_log",
    "score_pp_accuracy_interaction",
    "score_match_quality",
    "map_ranked_status",
    "map_max_combo_log",
    "map_drain_seconds_log",
]

STATIC_FEATURE_NAMES_V2 = (
    [
        "star",
        "map_accuracy_fraction",
        "accuracy_gap_fraction",
        "log_length_seconds",
        "star_squared",
        "star_times_accuracy",
        "star_times_accuracy_squared",
        "log_accuracy_gap",
    ]
    + [f"mod_{token}" for token in MODEL_MOD_TOKENS_V2]
    + [f"replay_{name}" for name in REPLAY_SUMMARY_NAMES]
    + MAP_AWARE_FEATURE_NAMES
    + SCORE_CONTEXT_FEATURE_NAMES
)


def parse_osu_beatmap(text: str | bytes | None) -> dict:
    """Parse the subset of a .osu file needed for map-aware replay features.

    The parser is deliberately dependency-free and tolerant.  Hit objects are
    returned as an ``N x 4`` float array with columns ``time_seconds, x, y,
    type_mask``.  Timing-point BPM is based only on uninherited positive beat
    lengths.
    """
    if text is None:
        return {
            "available": False,
            "difficulty": {},
            "general": {},
            "objects": np.zeros((0, 4), dtype=np.float64),
            "bpms": np.zeros(0, dtype=np.float64),
        }
    if isinstance(text, bytes):
        decoded = text.decode("utf-8-sig", errors="replace")
    else:
        decoded = str(text)

    section = ""
    difficulty: dict[str, float] = {}
    general: dict[str, str] = {}
    objects: list[tuple[float, float, float, float]] = []
    bpms: list[float] = []

    for raw_line in decoded.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue

        if section in {"difficulty", "general"} and ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if section == "difficulty":
                try:
                    difficulty[key] = float(value)
                except ValueError:
                    pass
            else:
                general[key] = value
            continue

        if section == "timingpoints":
            fields = line.split(",")
            if len(fields) >= 2:
                try:
                    beat_length = float(fields[1])
                    uninherited = int(fields[6]) if len(fields) > 6 else 1
                except (TypeError, ValueError):
                    continue
                if uninherited == 1 and beat_length > 0:
                    bpm = 60_000.0 / beat_length
                    if np.isfinite(bpm) and 20.0 <= bpm <= 1000.0:
                        bpms.append(float(bpm))
            continue

        if section == "hitobjects":
            fields = line.split(",")
            if len(fields) < 5:
                continue
            try:
                x = float(fields[0])
                y = float(fields[1])
                time_seconds = float(fields[2]) / 1000.0
                type_mask = float(int(fields[3]))
            except (TypeError, ValueError, OverflowError):
                continue
            if all(np.isfinite(value) for value in (x, y, time_seconds)):
                objects.append((time_seconds, x, y, type_mask))

    object_array = np.asarray(objects, dtype=np.float64)
    if object_array.size == 0:
        object_array = np.zeros((0, 4), dtype=np.float64)
    else:
        object_array = object_array[np.argsort(object_array[:, 0], kind="stable")]

    return {
        "available": bool(len(object_array)),
        "difficulty": difficulty,
        "general": general,
        "objects": object_array,
        "bpms": np.asarray(bpms, dtype=np.float64),
    }


def _speed_rate_from_mods(mods: list[str] | tuple[str, ...] | set[str]) -> float:
    enabled = {str(token).upper() for token in mods}
    if "DT" in enabled or "NC" in enabled:
        return 1.5
    if "HT" in enabled:
        return 0.75
    return 1.0


def _effective_difficulty_values(
    ar: float,
    od: float,
    cs: float,
    hp: float,
    mods: list[str] | tuple[str, ...] | set[str],
) -> dict[str, float]:
    """Apply stable's EZ/HR and clock-rate transforms to map difficulty."""
    enabled = {str(token).upper() for token in mods}
    multiplier = 0.5 if "EZ" in enabled else (1.4 if "HR" in enabled else 1.0)
    ar_mod = min(10.0, max(0.0, ar * multiplier))
    od_mod = min(10.0, max(0.0, od * multiplier))
    hp_mod = min(10.0, max(0.0, hp * multiplier))
    cs_multiplier = 0.5 if "EZ" in enabled else (1.3 if "HR" in enabled else 1.0)
    cs_mod = min(10.0, max(0.0, cs * cs_multiplier))
    rate = _speed_rate_from_mods(enabled)

    ar_preempt = (1800.0 - 120.0 * ar_mod) if ar_mod < 5.0 else (1950.0 - 150.0 * ar_mod)
    ar_preempt /= rate
    ar_effective = (
        (1800.0 - ar_preempt) / 120.0
        if ar_preempt > 1200.0
        else 5.0 + (1200.0 - ar_preempt) / 150.0
    )
    od_window_300 = (79.5 - 6.0 * od_mod) / rate
    od_effective = (79.5 - od_window_300) / 6.0
    circle_radius = max(8.0, 54.4 - 4.48 * cs_mod)
    return {
        "ar": float(ar_effective),
        "od": float(od_effective),
        "cs": float(cs_mod),
        "hp": float(hp_mod),
        "ar_preempt_ms": float(ar_preempt),
        "od_window_300_ms": float(od_window_300),
        "circle_radius": float(circle_radius),
    }


def _extract_press_times(events: np.ndarray) -> np.ndarray:
    events = collapse_duplicate_timestamps(np.asarray(events, dtype=np.float64))
    if len(events) < 2:
        return np.zeros(0, dtype=np.float64)
    times = events[:, 0] - events[0, 0]
    left, right = key_states_from_masks(events[:, 3].astype(np.int64))
    left_press = np.diff(left.astype(np.int8), prepend=0) == 1
    right_press = np.diff(right.astype(np.int8), prepend=0) == 1
    press_times = np.concatenate((times[left_press], times[right_press]))
    if not len(press_times):
        return np.zeros(0, dtype=np.float64)
    return np.sort(press_times.astype(np.float64))


def _nearest_values(sorted_values: np.ndarray, query: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return nearest value and source index for each query."""
    sorted_values = np.asarray(sorted_values, dtype=np.float64)
    query = np.asarray(query, dtype=np.float64)
    if not len(sorted_values) or not len(query):
        return np.zeros(len(query), dtype=np.float64), np.full(len(query), -1, dtype=np.int64)
    right = np.searchsorted(sorted_values, query, side="left")
    left = np.clip(right - 1, 0, len(sorted_values) - 1)
    right = np.clip(right, 0, len(sorted_values) - 1)
    choose_right = np.abs(sorted_values[right] - query) < np.abs(sorted_values[left] - query)
    indices = np.where(choose_right, right, left)
    return sorted_values[indices], indices.astype(np.int64)


def _monotonic_press_alignment(
    aligned_object_times: np.ndarray,
    press_times: np.ndarray,
    tolerance: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Uniquely align objects to presses in temporal order.

    Standard replays do not carry hit-object IDs.  A monotonic one-to-one
    assignment is a safer proxy than independently reusing the same key press
    for several nearby objects.
    """
    aligned_object_times = np.asarray(aligned_object_times, dtype=np.float64)
    press_times = np.asarray(press_times, dtype=np.float64)
    matched_times = np.full(len(aligned_object_times), np.nan, dtype=np.float64)
    matched_indices = np.full(len(aligned_object_times), -1, dtype=np.int64)
    minimum_index = 0
    for object_index, object_time in enumerate(aligned_object_times):
        if minimum_index >= len(press_times):
            break
        insertion = int(np.searchsorted(press_times, object_time, side="left"))
        candidates = {
            max(minimum_index, insertion - 2),
            max(minimum_index, insertion - 1),
            max(minimum_index, insertion),
            max(minimum_index, insertion + 1),
        }
        candidates = [index for index in candidates if minimum_index <= index < len(press_times)]
        if not candidates:
            continue
        best = min(candidates, key=lambda index: abs(press_times[index] - object_time))
        if abs(press_times[best] - object_time) <= tolerance:
            matched_times[object_index] = press_times[best]
            matched_indices[object_index] = best
            minimum_index = best + 1
    return matched_times, matched_indices


def _safe_corr(left: np.ndarray, right: np.ndarray) -> float:
    left = np.asarray(left, dtype=np.float64)
    right = np.asarray(right, dtype=np.float64)
    mask = np.isfinite(left) & np.isfinite(right)
    left = left[mask]
    right = right[mask]
    if len(left) < 3 or np.std(left) < 1e-12 or np.std(right) < 1e-12:
        return 0.0
    return float(np.corrcoef(left, right)[0, 1])


def build_map_aware_features(
    parsed_replay: dict,
    beatmap: dict | None,
    *,
    mods: list[str] | tuple[str, ...] | set[str],
    star: float,
    length_seconds: float,
) -> np.ndarray:
    """Build map/replay alignment features, including UR and aim-error proxies.

    Exact hit errors are not stored directly in .osr.  The timing features are
    therefore explicitly *proxies*: key-downs are monotonically aligned to hit
    object start times, then robust residual statistics are computed.  This is
    materially more informative than header accuracy alone while remaining
    reproducible at serving time.
    """
    values = {name: 0.0 for name in MAP_AWARE_FEATURE_NAMES}
    if not beatmap or not beatmap.get("available"):
        return np.asarray([values[name] for name in MAP_AWARE_FEATURE_NAMES], dtype=np.float32)

    objects = np.asarray(beatmap.get("objects"), dtype=np.float64)
    if objects.ndim != 2 or objects.shape[1] < 4 or len(objects) < 2:
        return np.asarray([values[name] for name in MAP_AWARE_FEATURE_NAMES], dtype=np.float32)

    rate = _speed_rate_from_mods(mods)
    object_times = objects[:, 0] / rate
    object_times = object_times - object_times[0]
    object_x = objects[:, 1]
    object_y = objects[:, 2]
    object_type = objects[:, 3].astype(np.int64)
    duration = max(float(length_seconds), float(object_times[-1]), 1e-3)

    difficulty = beatmap.get("difficulty") or {}
    ar_base = float(difficulty.get("ApproachRate", difficulty.get("OverallDifficulty", 0.0)) or 0.0)
    od_base = float(difficulty.get("OverallDifficulty", 0.0) or 0.0)
    cs_base = float(difficulty.get("CircleSize", 0.0) or 0.0)
    hp_base = float(difficulty.get("HPDrainRate", 0.0) or 0.0)
    effective = _effective_difficulty_values(ar_base, od_base, cs_base, hp_base, mods)
    ar = effective["ar"]
    od = effective["od"]
    cs = effective["cs"]
    hp = effective["hp"]
    slider_multiplier = float(difficulty.get("SliderMultiplier", 0.0) or 0.0)
    bpms = np.asarray(beatmap.get("bpms", []), dtype=np.float64) * rate
    bpm = safe_quantile(bpms, 0.5) if len(bpms) else 0.0

    intervals = np.diff(object_times)
    positive_intervals = intervals[intervals > 1e-5]
    jumps = np.hypot(np.diff(object_x), np.diff(object_y))
    vectors = np.column_stack((np.diff(object_x), np.diff(object_y)))
    if len(vectors) >= 2:
        v1 = vectors[:-1]
        v2 = vectors[1:]
        denom = np.linalg.norm(v1, axis=1) * np.linalg.norm(v2, axis=1)
        cosines = np.sum(v1 * v2, axis=1) / np.maximum(denom, 1e-6)
        angles = np.arccos(np.clip(cosines, -1.0, 1.0)) / math.pi
    else:
        angles = np.zeros(0, dtype=np.float64)

    circles = (object_type & 1) != 0
    sliders = (object_type & 2) != 0
    spinners = (object_type & 8) != 0

    values.update({
        "beatmap_available": 1.0,
        "map_ar_base": ar_base,
        "map_od_base": od_base,
        "map_cs_base": cs_base,
        "map_hp_base": hp_base,
        "map_ar": ar,
        "map_od": od,
        "map_cs": cs,
        "map_hp": hp,
        "map_ar_preempt_ms": effective["ar_preempt_ms"],
        "map_od_hit_window_300_ms": effective["od_window_300_ms"],
        "map_circle_radius": effective["circle_radius"],
        "map_bpm_log": math.log1p(max(bpm, 0.0)),
        "map_slider_multiplier": slider_multiplier,
        "map_object_count_log": math.log1p(len(objects)),
        "map_circle_fraction": float(circles.mean()),
        "map_slider_fraction": float(sliders.mean()),
        "map_spinner_fraction": float(spinners.mean()),
        "map_objects_per_second": len(objects) / duration,
        "map_interval_mean": safe_mean(positive_intervals),
        "map_interval_std": safe_std(positive_intervals),
        "map_interval_q10": safe_quantile(positive_intervals, 0.10),
        "map_interval_q50": safe_quantile(positive_intervals, 0.50),
        "map_interval_q90": safe_quantile(positive_intervals, 0.90),
        "map_stream_fraction_125ms": float(np.mean(positive_intervals <= 0.125)) if len(positive_intervals) else 0.0,
        "map_stream_fraction_100ms": float(np.mean(positive_intervals <= 0.100)) if len(positive_intervals) else 0.0,
        "map_stream_fraction_80ms": float(np.mean(positive_intervals <= 0.080)) if len(positive_intervals) else 0.0,
        "map_jump_mean_log": math.log1p(max(safe_mean(jumps), 0.0)),
        "map_jump_std_log": math.log1p(max(safe_std(jumps), 0.0)),
        "map_jump_q50_log": math.log1p(max(safe_quantile(jumps, 0.50), 0.0)),
        "map_jump_q90_log": math.log1p(max(safe_quantile(jumps, 0.90), 0.0)),
        "map_jump_q99_log": math.log1p(max(safe_quantile(jumps, 0.99), 0.0)),
        "map_angle_mean": safe_mean(angles),
        "map_angle_std": safe_std(angles),
        "map_angle_q10": safe_quantile(angles, 0.10),
        "map_angle_q50": safe_quantile(angles, 0.50),
        "map_angle_q90": safe_quantile(angles, 0.90),
        "map_density_star_interaction": (len(objects) / duration) * max(float(star), 0.0),
    })

    events = collapse_duplicate_timestamps(np.asarray(parsed_replay.get("events", []), dtype=np.float64))
    press_times = _extract_press_times(events)
    values["press_object_ratio"] = len(press_times) / max(len(objects), 1)
    if len(press_times) < 2 or len(events) < 2:
        return np.nan_to_num(
            np.asarray([values[name] for name in MAP_AWARE_FEATURE_NAMES], dtype=np.float32),
            nan=0.0, posinf=1e6, neginf=-1e6,
        )

    # Align the first key press to the first object, then refine by the median
    # nearest-neighbour residual.  Two refinement rounds are enough for clock
    # offsets while avoiding expensive dynamic programming on long maps.
    aligned_object_times = object_times + press_times[0]
    total_shift = 0.0
    for _ in range(2):
        nearest, _ = _nearest_values(press_times, aligned_object_times)
        residual = nearest - aligned_object_times
        good = np.abs(residual) <= 0.250
        if not np.any(good):
            break
        shift = float(np.median(residual[good]))
        aligned_object_times += shift
        total_shift += shift

    hit_window = max(0.060, min(0.200, (199.5 - 10.0 * od) / 1000.0))
    nearest_press, nearest_indices = _monotonic_press_alignment(
        aligned_object_times,
        press_times,
        tolerance=max(hit_window * 2.25, 0.150),
    )
    timing_residual = nearest_press - aligned_object_times
    matched = np.isfinite(timing_residual) & (np.abs(timing_residual) <= max(hit_window * 1.8, 0.120))
    matched_residual = timing_residual[matched]
    matched_residual_ms = matched_residual * 1000.0
    centered_ms = matched_residual_ms - safe_quantile(matched_residual_ms, 0.5)
    abs_centered_ms = np.abs(centered_ms)

    values.update({
        "timing_match_fraction": float(matched.mean()),
        "timing_offset_ms": total_shift * 1000.0,
        "timing_residual_mean_ms": safe_mean(centered_ms),
        "timing_residual_abs_mean_ms": safe_mean(abs_centered_ms),
        "timing_residual_std_ms": safe_std(centered_ms),
        "timing_residual_q50_abs_ms": safe_quantile(abs_centered_ms, 0.50),
        "timing_residual_q90_abs_ms": safe_quantile(abs_centered_ms, 0.90),
        "timing_residual_q99_abs_ms": safe_quantile(abs_centered_ms, 0.99),
        "timing_ur_proxy_log": math.log1p(max(10.0 * safe_std(centered_ms), 0.0)),
        "timing_early_fraction": float(np.mean(centered_ms < -15.0)) if len(centered_ms) else 0.0,
        "timing_late_fraction": float(np.mean(centered_ms > 15.0)) if len(centered_ms) else 0.0,
    })

    # Cursor position and speed at each aligned object time.
    event_times = events[:, 0] - events[0, 0]
    x = events[:, 1]
    y = events[:, 2]
    event_dt = np.diff(event_times, prepend=event_times[0])
    positive_dt = event_dt[event_dt > 1e-5]
    event_dt[0] = float(np.median(positive_dt)) if len(positive_dt) else 1.0 / 60.0
    event_dt = np.clip(event_dt, 1e-4, 1.0)
    speed = np.hypot(np.diff(x, prepend=x[0]), np.diff(y, prepend=y[0])) / event_dt
    replay_object_times = np.where(matched, nearest_press, aligned_object_times)
    replay_object_times = np.clip(replay_object_times, event_times[0], event_times[-1])
    cursor_x = np.interp(replay_object_times, event_times, x)
    cursor_y = np.interp(replay_object_times, event_times, y)
    cursor_speed = np.interp(replay_object_times, event_times, speed)
    dx = cursor_x - object_x
    dy = cursor_y - object_y
    aim_error = np.hypot(dx, dy)
    circle_radius = effective["circle_radius"]
    aim_radius = aim_error / circle_radius
    aim_valid = matched & (~spinners) & np.isfinite(aim_error)
    aim_error_valid = aim_error[aim_valid]
    aim_radius_valid = aim_radius[aim_valid]
    speed_valid = cursor_speed[aim_valid]
    dx_valid = dx[aim_valid]
    dy_valid = dy[aim_valid]

    values.update({
        "aim_match_fraction": float(aim_valid.mean()),
        "aim_error_mean_log": math.log1p(max(safe_mean(aim_error_valid), 0.0)),
        "aim_error_q50_log": math.log1p(max(safe_quantile(aim_error_valid, 0.50), 0.0)),
        "aim_error_q90_log": math.log1p(max(safe_quantile(aim_error_valid, 0.90), 0.0)),
        "aim_error_q99_log": math.log1p(max(safe_quantile(aim_error_valid, 0.99), 0.0)),
        "aim_error_radius_mean": safe_mean(aim_radius_valid),
        "aim_error_radius_q90": safe_quantile(aim_radius_valid, 0.90),
        "aim_inside_circle_fraction": float(np.mean(aim_radius_valid <= 1.0)) if len(aim_radius_valid) else 0.0,
        "aim_bias_x": safe_mean(dx_valid) / 512.0,
        "aim_bias_y": safe_mean(dy_valid) / 384.0,
        "cursor_speed_at_object_q50_log": math.log1p(max(safe_quantile(speed_valid, 0.50), 0.0)),
        "cursor_speed_at_object_q90_log": math.log1p(max(safe_quantile(speed_valid, 0.90), 0.0)),
        "cursor_speed_at_object_q99_log": math.log1p(max(safe_quantile(speed_valid, 0.99), 0.0)),
    })

    if len(press_times) >= 3 and len(positive_intervals) >= 2:
        press_intervals = np.diff(press_times)
        sample_count = min(len(press_intervals), len(positive_intervals))
        press_sample = press_intervals[:sample_count]
        object_sample = positive_intervals[:sample_count]
        log_ratio = np.log(np.maximum(press_sample, 1e-4) / np.maximum(object_sample, 1e-4))
        values["rhythm_interval_log_ratio_mean"] = safe_mean(log_ratio)
        values["rhythm_interval_log_ratio_std"] = safe_std(log_ratio)
        values["rhythm_interval_corr"] = _safe_corr(press_sample, object_sample)

    replay_path_length = float(np.hypot(np.diff(x), np.diff(y)).sum())
    object_path_length = float(jumps.sum())
    values["object_cursor_path_ratio_log"] = math.log1p(replay_path_length / max(object_path_length, 1e-6))

    return np.nan_to_num(
        np.asarray([values[name] for name in MAP_AWARE_FEATURE_NAMES], dtype=np.float32),
        nan=0.0, posinf=1e6, neginf=-1e6,
    )


def build_static_features_v2(
    parsed_replay: dict,
    *,
    star: float,
    accuracy: float,
    length_seconds: float,
    model_mods: list[str],
    beatmap: dict | None = None,
    score_pp: float | None = None,
    score_match_quality: float = 0.0,
    map_ranked_status: float | int | None = None,
    map_max_combo: float | int | None = None,
    map_drain_seconds: float | int | None = None,
) -> np.ndarray:
    accuracy = float(np.clip(accuracy, 0.0, 1.0))
    star = max(float(star), 0.0)
    length_seconds = max(float(length_seconds), 0.0)
    gap = 1.0 - accuracy
    enabled = {str(token).upper() for token in model_mods}

    base = [
        star,
        accuracy,
        gap,
        math.log1p(length_seconds),
        star ** 2,
        star * accuracy,
        star * accuracy ** 2,
        math.log1p(gap * 100.0),
    ]
    base.extend(1.0 if token in enabled else 0.0 for token in MODEL_MOD_TOKENS_V2)

    replay_summary = build_replay_summary(parsed_replay).astype(np.float32)
    map_features = build_map_aware_features(
        parsed_replay,
        beatmap,
        mods=model_mods,
        star=star,
        length_seconds=length_seconds,
    )

    header = parsed_replay.get("header") or {}
    total_hits = max(
        1,
        int(header.get("count_300", 0))
        + int(header.get("count_100", 0))
        + int(header.get("count_50", 0))
        + int(header.get("count_miss", 0)),
    )
    pp_value = float(score_pp) if score_pp is not None and np.isfinite(score_pp) and score_pp >= 0 else 0.0
    pp_available = 1.0 if pp_value > 0 else 0.0
    score_context = np.asarray(
        [
            pp_available,
            math.log1p(pp_value),
            pp_value / max(star, 0.5) / 100.0,
            math.log1p(pp_value / total_hits),
            math.log1p(pp_value) * accuracy,
            float(np.clip(score_match_quality, 0.0, 1.0)),
            float(map_ranked_status or 0.0),
            math.log1p(max(float(map_max_combo or 0.0), 0.0)),
            math.log1p(max(float(map_drain_seconds or 0.0), 0.0)),
        ],
        dtype=np.float32,
    )

    static = np.concatenate(
        (
            np.asarray(base, dtype=np.float32),
            replay_summary,
            map_features,
            score_context,
        )
    )
    if len(static) != len(STATIC_FEATURE_NAMES_V2):
        raise RuntimeError(
            f"Static v2 feature mismatch: {len(static)} != {len(STATIC_FEATURE_NAMES_V2)}"
        )
    return np.nan_to_num(static, nan=0.0, posinf=1e6, neginf=-1e6).astype(np.float32)
