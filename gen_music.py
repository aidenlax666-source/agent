import wave
import math
import struct

SAMPLE_RATE = 44100
DURATION = 45.0
AMPLITUDE = 0.5
FADE_IN = 0.5
FADE_OUT = 2.0

NOTE_FREQS = {
    'C': 0, 'C#': 1, 'D': 2, 'D#': 3, 'E': 4, 'F': 5,
    'F#': 6, 'G': 7, 'G#': 8, 'A': 9, 'A#': 10, 'B': 11
}

def midi_to_freq(midi):
    return 440.0 * (2 ** ((midi - 69) / 12.0))

def note_to_midi(note_name, octave):
    base = NOTE_FREQS[note_name]
    return base + (octave + 1) * 12

def note_freq(note_name, octave):
    return midi_to_freq(note_to_midi(note_name, octave))

def envelope(attack, decay, total_samples, sample_idx):
    if sample_idx < attack * SAMPLE_RATE:
        return sample_idx / (attack * SAMPLE_RATE)
    elif sample_idx < (attack + decay) * SAMPLE_RATE:
        t = (sample_idx - attack * SAMPLE_RATE) / (decay * SAMPLE_RATE)
        return 1.0 - t * 0.7
    else:
        return 0.3

def synth_note(freq, duration, attack=0.01, decay=0.3):
    n_samples = int(duration * SAMPLE_RATE)
    samples = []
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        env = envelope(attack, decay, n_samples, i)
        # Add slight vibrato for brightness
        vibrato = 1.0 + 0.003 * math.sin(2 * math.pi * 5.0 * t)
        val = AMPLITUDE * env * math.sin(2 * math.pi * freq * vibrato * t)
        samples.append(val)
    return samples

def synth_bass(freq, duration, attack=0.005, decay=0.2):
    n_samples = int(duration * SAMPLE_RATE)
    samples = []
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        env = envelope(attack, decay, n_samples, i)
        # Add second harmonic for warmth
        val = AMPLITUDE * 0.6 * env * (
            math.sin(2 * math.pi * freq * t) +
            0.3 * math.sin(2 * math.pi * freq * 2 * t)
        )
        samples.append(val)
    return samples

def add_to_buffer(buffer, samples, start_sample):
    for i, s in enumerate(samples):
        idx = start_sample + i
        if idx < len(buffer):
            buffer[idx] += s

def main():
    # Song structure: 45 seconds, 100 BPM => 0.6s per beat
    beat = 0.6
    total_samples = int(DURATION * SAMPLE_RATE)
    buffer = [0.0] * total_samples

    # Melody: (note, octave, duration_in_beats, rest_after)
    melody = [
        ('C', 5, 1.0, 0.5), ('E', 5, 1.0, 0.5), ('G', 5, 1.0, 0.5), ('A', 5, 1.0, 0.5),
        ('G', 5, 0.5, 0.25), ('E', 5, 0.5, 0.25), ('C', 5, 1.0, 0.5), ('D', 5, 1.0, 0.5),
        ('E', 5, 1.0, 0.5), ('G', 5, 1.0, 0.5), ('A', 5, 1.0, 0.5), ('C', 6, 1.0, 0.5),
        ('A', 5, 0.5, 0.25), ('G', 5, 0.5, 0.25), ('E', 5, 1.0, 0.5), ('D', 5, 1.0, 0.5),
        ('C', 5, 2.0, 1.0), ('G', 4, 1.0, 0.5), ('A', 4, 1.0, 0.5), ('B', 4, 1.0, 0.5),
        ('C', 5, 1.0, 0.5), ('D', 5, 1.0, 0.5), ('E', 5, 1.0, 0.5), ('G', 5, 1.0, 0.5),
        ('E', 5, 0.5, 0.25), ('D', 5, 0.5, 0.25), ('C', 5, 1.0, 0.5), ('A', 4, 1.0, 0.5),
        ('G', 4, 2.0, 1.0), ('C', 5, 1.0, 0.5), ('E', 5, 1.0, 0.5), ('G', 5, 1.0, 0.5),
        ('A', 5, 1.0, 0.5), ('G', 5, 0.5, 0.25), ('E', 5, 0.5, 0.25), ('C', 5, 1.0, 0.5),
        ('D', 5, 1.0, 0.5), ('E', 5, 1.0, 0.5), ('C', 5, 1.0, 0.5), ('G', 4, 1.0, 0.5),
        ('A', 4, 1.0, 0.5), ('C', 5, 1.0, 0.5), ('D', 5, 1.0, 0.5), ('E', 5, 2.0, 1.0),
        ('C', 5, 1.0, 0.5), ('G', 4, 1.0, 0.5), ('A', 4, 1.0, 0.5), ('C', 5, 1.0, 0.5),
        ('D', 5, 1.0, 0.5), ('E', 5, 1.0, 0.5), ('G', 5, 1.0, 0.5), ('A', 5, 1.0, 0.5),
        ('G', 5, 0.5, 0.25), ('E', 5, 0.5, 0.25), ('C', 5, 2.0, 1.0)
    ]

    # Bass line (simpler, follows chord progression)
    bass = [
        ('C', 3, 4.0), ('G', 3, 4.0), ('A', 3, 4.0), ('F', 3, 4.0),
        ('C', 3, 4.0), ('G', 3, 4.0), ('A', 3, 4.0), ('F', 3, 4.0),
        ('C', 3, 4.0), ('G', 3, 4.0), ('A', 3, 4.0), ('F', 3, 4.0),
        ('C', 3, 4.0), ('G', 3, 4.0), ('A', 3, 4.0), ('F', 3, 4.0),
        ('C', 3, 4.0), ('G', 3, 4.0), ('A', 3, 4.0), ('F', 3, 4.0),
        ('C', 3, 4.0), ('G', 3, 4.0), ('A', 3, 4.0), ('F', 3, 4.0),
        ('C', 3, 4.0), ('G', 3, 4.0), ('A', 3, 4.0), ('F', 3, 4.0),
        ('C', 3, 4.0), ('G', 3, 4.0), ('A', 3, 4.0), ('F', 3, 4.0),
        ('C', 3, 4.0), ('G', 3, 4.0), ('A', 3, 4.0), ('F', 3, 4.0),
        ('C', 3, 4.0), ('G', 3, 4.0), ('A', 3, 4.0), ('F', 3, 4.0),
        ('C', 3, 4.0), ('G', 3, 4.0), ('A', 3, 4.0), ('F', 3, 4.0),
        ('C', 3, 4.0), ('G', 3, 4.0), ('A', 3, 4.0), ('F', 3, 4.0)
    ]

    # Add melody
    current_time = 0.0
    note_count = 0
    for note_name, octave, dur_beats, rest_beats in melody:
        freq = note_freq(note_name, octave)
        note_dur = dur_beats * beat
        samples = synth_note(freq, note_dur)
        start_sample = int(current_time * SAMPLE_RATE)
        add_to_buffer(buffer, samples, start_sample)
        current_time += (dur_beats + rest_beats) * beat
        note_count += 1

    # Add bass
    current_time = 0.0
    for note_name, octave, dur_beats in bass:
        freq = note_freq(note_name, octave)
        note_dur = dur_beats * beat
        samples = synth_bass(freq, note_dur)
        start_sample = int(current_time * SAMPLE_RATE)
        add_to_buffer(buffer, samples, start_sample)
        current_time += dur_beats * beat

    # Apply fade in/out
    fade_in_samples = int(FADE_IN * SAMPLE_RATE)
    fade_out_samples = int(FADE_OUT * SAMPLE_RATE)
    for i in range(total_samples):
        if i < fade_in_samples:
            buffer[i] *= i / fade_in_samples
        elif i > total_samples - fade_out_samples:
            buffer[i] *= (total_samples - i) / fade_out_samples

    # Normalize to peak 0.5
    peak = max(abs(s) for s in buffer)
    if peak > 0.5:
        scale = 0.5 / peak
        buffer = [s * scale for s in buffer]

    # Write WAV file
    with wave.open('melody.wav', 'w') as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(SAMPLE_RATE)
        frames = bytearray()
        for s in buffer:
            # Convert to 16-bit PCM
            val = int(max(-1.0, min(1.0, s)) * 32767)
            frames.extend(struct.pack('<h', val))
        wav_file.writeframes(bytes(frames))

    print(f"DURATION:{DURATION:.1f}")
    print(f"NOTES:{note_count}")

if __name__ == "__main__":
    main()