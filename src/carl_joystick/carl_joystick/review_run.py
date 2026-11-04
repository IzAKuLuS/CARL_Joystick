"""Read-only run summary and CSV timeline. No ROS or third-party dependencies."""
import argparse
from collections import Counter
import csv
import itertools
import json
from pathlib import Path
import sys


def records(directory):
    for path in sorted(Path(directory).glob('events-*.jsonl')):
        with path.open() as stream:
            for number, line in enumerate(stream, 1):
                try:
                    item = json.loads(line)
                    if (not isinstance(item, dict) or not isinstance(item.get('event'), str)
                            or not isinstance(item.get('data', {}), dict)):
                        raise ValueError('Invalid record')
                    yield item
                except ValueError:
                    yield dict(event='unreadable_record', data={'file': path.name, 'line': number})


def browser_records(paths):
    for path in paths:
        with Path(path).open() as stream:
            for number, line in enumerate(stream, 1):
                try:
                    browser = json.loads(line)
                    if not isinstance(browser, dict):
                        raise ValueError('Expected event')
                    yield dict(event='browser_event', data={'browser': browser, 'offline_file': str(path)})
                except ValueError:
                    yield dict(event='unreadable_record', data={'file': str(path), 'line': number})


def review(directory, timeline=None, browser_logs=()):
    events, stops, browser_events = Counter(), Counter(), Counter()
    previous_seq = None
    gaps = dropped = 0
    last_state = {}
    browser_ids = set()
    feedback = {}
    historical_browser_events = 0
    summary = dict(run_id=None, start=None, end=None, clean_shutdown=False)
    writer = None
    if timeline:
        writer = csv.writer(timeline)
        writer.writerow(['server_utc', 'server_mono_ns', 'event', 'session_id',
                         'browser_wall_ms', 'browser_mono_ms', 'details_json'])
    for item in itertools.chain(records(directory), browser_records(browser_logs)):
        event, data = item['event'], item.get('data', {})
        events[event] += 1
        if item.get('run_id'):
            summary['run_id'] = item['run_id']
            summary['start'] = summary['start'] or item.get('utc')
            summary['end'] = item.get('utc')
        seq = item.get('seq')
        if seq is not None:
            if previous_seq is not None and seq > previous_seq + 1:
                gaps += seq - previous_seq - 1
            previous_seq = seq
        dropped = max(dropped, item.get('dropped_total', 0), data.get('logging', {}).get('dropped', 0))
        include = event in {'run_start', 'run_end', 'session_connected', 'session_closed', 'session_rejected',
                            'session_timeout', 'websocket_disconnect', 'readiness_changed', 'packet_error',
                            'server_error', 'process_error', 'executor_error', 'shutdown_zero_error',
                            'command_publish_error', 'estop_publish_error',
                            'estop_published', 'control_timer_late', 'unreadable_record'}
        if event == 'run_end':
            summary['clean_shutdown'] = True
        if event == 'motion_stop' and data.get('was_active'):
            stops[data['reason']] += 1
            include = True
        if event == 'command_result':
            state = data.get('state', {})
            include = state.get('active') != last_state.get('active') or bool(state.get('rejection'))
            last_state = state
        if event == 'telemetry' and data.get('topic') in ('heartbeat', 'throttle_status'):
            topic = data['topic']
            message = data.get('message', {})
            fields = ('flags', 'mode') if topic == 'heartbeat' else ('state', 'faults', 'applied_left', 'applied_right')
            signature = tuple(message.get(key) for key in fields)
            include = feedback.get(topic) != signature
            feedback[topic] = signature
        if event == 'telemetry' and data.get('topic') == 'encoders':
            message = data.get('message', {})
            counts = (message.get('rear_left_counts'), message.get('rear_right_counts'))
            previous = feedback.get('encoder_counts')
            if previous is not None:
                changing = previous != counts
                include = feedback.get('encoder_changing') != changing
                feedback['encoder_changing'] = changing
            feedback['encoder_counts'] = counts
        if event == 'telemetry' and data.get('topic') == 'bridge_diagnostics':
            try:
                bridge = json.loads(data['message']['data'])
                include = bridge['event'] in ('transport_error', 'transport_open', 'transport_closed')
            except (ValueError, KeyError, TypeError):
                pass
        if event == 'browser_event':
            browser = data.get('browser', {})
            identifier = browser.get('id')
            if not isinstance(identifier, str):
                identifier = None
            if identifier and identifier in browser_ids:
                continue
            if identifier:
                browser_ids.add(identifier)
            name = browser.get('event', 'unknown')
            if not isinstance(name, str):
                name = 'invalid_event_name'
            original_run = browser.get('data', {}).get('run_id') if isinstance(browser.get('data'), dict) else None
            if original_run and original_run != summary['run_id']:
                historical_browser_events += 1
            else:
                browser_events[name] += 1
            include = name not in ('touch_move', 'command_send', 'reply_received', 'server_http_probe')
        if writer and include:
            browser = data.get('browser', {})
            writer.writerow([item.get('utc'), item.get('mono_ns'), event, data.get('session_id'),
                             browser.get('wall_ms'), browser.get('mono_ms'),
                             json.dumps(data, separators=(',', ':'))])
    summary['run_end_present'] = summary['clean_shutdown']
    summary['clean_shutdown'] = summary['clean_shutdown'] and not any(
        events[name] for name in ('process_error', 'executor_error', 'shutdown_zero_error'))
    summary.update(events=dict(events), active_motion_stops=dict(stops),
                   browser_events=dict(browser_events), historical_browser_events=historical_browser_events, missing_sequences=gaps, dropped_total=dropped,
                   note='Active means accepted joystick control, not proven vehicle motion. '
                        'Inspect applied outputs and encoder changes. Browser times use a separate clock. '
                        'Missing sequences can reflect queue loss or retention rotation.')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', help='One test-run directory')
    parser.add_argument('--timeline', help='Write a CSV timeline of transitions/errors')
    parser.add_argument('--browser-log', action='append', default=[], help='Merge a downloaded browser JSONL; repeatable')
    args = parser.parse_args()
    if not list(Path(args.directory).glob('events-*.jsonl')):
        parser.error('No events-*.jsonl files in this directory')
    if args.timeline:
        with Path(args.timeline).open('w', newline='') as stream:
            result = review(args.directory, stream, args.browser_log)
    else:
        result = review(args.directory, browser_logs=args.browser_log)
    json.dump(result, sys.stdout, indent=2)
    print()


if __name__ == '__main__':
    main()
