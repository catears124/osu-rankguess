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
