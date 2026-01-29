#!/usr/bin/env python3
"""
AVC-LAN Log Analyzer.

Analyzes AVC-LAN NDJSON log files to identify patterns, frequent messages,
and correlations between events.

Usage:
    python -m cyberpunk_computer.comm.analyze_log assets/data/avc_lan.ndjson
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

from .avc_decoder import AVCDecoder, AVCMessage


def load_log(path: Path) -> list[dict]:
    """Load messages from an NDJSON log file."""
    messages = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('>>>') or line.startswith('MPY:'):
                continue
            if line.startswith('('):  # Comments like "(power off)"
                continue
            try:
                msg = json.loads(line)
                if msg.get("id") == 2:  # Only AVC-LAN frames
                    messages.append(msg)
            except json.JSONDecodeError:
                continue
    return messages


def analyze_addresses(messages: list[dict], decoder: AVCDecoder) -> None:
    """Analyze device address frequency."""
    master_counts = Counter()
    slave_counts = Counter()
    pair_counts = Counter()
    
    for raw in messages:
        msg = decoder.decode_message(raw)
        if msg:
            master_counts[msg.master_addr] += 1
            slave_counts[msg.slave_addr] += 1
            pair_counts[(msg.master_addr, msg.slave_addr)] += 1
    
    print("\n" + "=" * 60)
    print("DEVICE ADDRESS ANALYSIS")
    print("=" * 60)
    
    print("\nTop 15 Master Addresses (senders):")
    print("-" * 40)
    for addr, count in master_counts.most_common(15):
        name = decoder._get_device_name(addr)
        print(f"  0x{addr:03X} ({name:20}) : {count:5} messages")
    
    print("\nTop 15 Slave Addresses (receivers):")
    print("-" * 40)
    for addr, count in slave_counts.most_common(15):
        name = decoder._get_device_name(addr)
        print(f"  0x{addr:03X} ({name:20}) : {count:5} messages")
    
    print("\nTop 20 Address Pairs (master → slave):")
    print("-" * 50)
    for (m, s), count in pair_counts.most_common(20):
        m_name = decoder._get_device_name(m)
        s_name = decoder._get_device_name(s)
        print(f"  {m_name:15} → {s_name:15} : {count:5} messages")


def analyze_data_patterns(messages: list[dict], decoder: AVCDecoder) -> None:
    """Analyze common data patterns."""
    pattern_counts = Counter()
    pattern_examples = {}
    
    for raw in messages:
        msg = decoder.decode_message(raw)
        if msg and len(msg.data) >= 4:
            # Create pattern from first 4 bytes
            pattern = tuple(msg.data[:4])
            pattern_counts[pattern] += 1
            if pattern not in pattern_examples:
                pattern_examples[pattern] = msg
    
    print("\n" + "=" * 60)
    print("DATA PATTERN ANALYSIS (first 4 bytes)")
    print("=" * 60)
    
    print("\nTop 25 Data Patterns:")
    print("-" * 60)
    for pattern, count in pattern_counts.most_common(25):
        hex_pattern = " ".join(f"{b:02X}" for b in pattern)
        example = pattern_examples[pattern]
        print(f"  [{hex_pattern}] : {count:4}x  "
              f"(e.g. {example.master_name} → {example.slave_name})")


def analyze_message_classification(messages: list[dict], decoder: AVCDecoder) -> None:
    """Analyze message classifications."""
    class_counts = Counter()
    class_examples = defaultdict(list)
    
    for raw in messages:
        msg = decoder.decode_message(raw)
        if msg:
            classification = decoder.classify_message(msg)
            class_counts[classification] += 1
            if len(class_examples[classification]) < 3:
                class_examples[classification].append(msg)
    
    print("\n" + "=" * 60)
    print("MESSAGE CLASSIFICATION")
    print("=" * 60)
    
    for classification, count in class_counts.most_common():
        print(f"\n{classification}: {count} messages")
        print("-" * 40)
        for msg in class_examples[classification]:
            print(f"  seq {msg.sequence:4}: "
                  f"{msg.master_name} → {msg.slave_name} "
                  f"[{msg.data_hex()[:30]}...]")


def analyze_sequence_range(
    messages: list[dict],
    decoder: AVCDecoder,
    start_seq: int,
    end_seq: int,
    label: str = ""
) -> None:
    """Analyze messages in a specific sequence range."""
    print(f"\n" + "=" * 60)
    print(f"SEQUENCE RANGE: {start_seq} - {end_seq}" + (f" ({label})" if label else ""))
    print("=" * 60)
    
    for raw in messages:
        seq = raw.get("seq", -1)
        if start_seq <= seq <= end_seq:
            msg = decoder.decode_message(raw)
            if msg:
                classification = decoder.classify_message(msg)
                print(f"seq {seq:4} [{classification:15}]: "
                      f"{msg.master_name:15} → {msg.slave_name:15} "
                      f"[{msg.data_hex()[:40]}]")


def find_unique_messages(messages: list[dict], decoder: AVCDecoder) -> None:
    """Find unique message patterns (appearing only once or twice)."""
    pattern_data = defaultdict(list)
    
    for raw in messages:
        msg = decoder.decode_message(raw)
        if msg:
            # Create signature from addresses and first 4 data bytes
            sig = (msg.master_addr, msg.slave_addr, tuple(msg.data[:4]))
            pattern_data[sig].append(msg)
    
    print("\n" + "=" * 60)
    print("RARE MESSAGES (1-2 occurrences)")
    print("=" * 60)
    
    rare_messages = [(sig, msgs) for sig, msgs in pattern_data.items() 
                     if len(msgs) <= 2]
    
    print(f"\nFound {len(rare_messages)} rare message patterns:")
    for sig, msgs in sorted(rare_messages, key=lambda x: x[1][0].sequence)[:30]:
        msg = msgs[0]
        print(f"  seq {msg.sequence:4}: "
              f"{msg.master_name:15} → {msg.slave_name:15} "
              f"[{msg.data_hex()[:40]}]")


def main() -> None:
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python -m cyberpunk_computer.comm.analyze_log <log_file.ndjson>")
        sys.exit(1)
    
    log_path = Path(sys.argv[1])
    if not log_path.exists():
        print(f"Error: File not found: {log_path}")
        sys.exit(1)
    
    print(f"Loading {log_path}...")
    messages = load_log(log_path)
    print(f"Loaded {len(messages)} AVC-LAN messages")
    
    decoder = AVCDecoder()
    
    # Run analyses
    analyze_addresses(messages, decoder)
    analyze_data_patterns(messages, decoder)
    analyze_message_classification(messages, decoder)
    
    # Analyze specific ranges from user's notes
    print("\n\n" + "#" * 60)
    print("SPECIFIC EVENT RANGES (from notes)")
    print("#" * 60)
    
    analyze_sequence_range(messages, decoder, 410, 420, "ICE Start area")
    analyze_sequence_range(messages, decoder, 425, 435, "MFD Buttons area")
    analyze_sequence_range(messages, decoder, 455, 465, "INFO mode area")
    analyze_sequence_range(messages, decoder, 510, 520, "AMP ON/OFF area")
    analyze_sequence_range(messages, decoder, 545, 555, "AUDIO/PARK area")
    analyze_sequence_range(messages, decoder, 595, 605, "Volume area")
    analyze_sequence_range(messages, decoder, 645, 655, "Touch events area")
    
    find_unique_messages(messages, decoder)


if __name__ == "__main__":
    main()
