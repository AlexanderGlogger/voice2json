#!/usr/bin/env python3

import sys
import re
import io
import os
import json
import time
import argparse
import logging
import tempfile
import subprocess
import threading
import shutil
import shlex
import platform
import itertools
from pathlib import Path
from collections import defaultdict
from typing import Set, Dict, Optional, List, Any, BinaryIO

logger = logging.getLogger("voice2json")

import yaml
import pydash
import jsonlines

from voice2json.utils import ppath, recursive_update

# -----------------------------------------------------------------------------


def main():
    # Parse command-line arguments
    parser = argparse.ArgumentParser(prog="voice2json", description="voice2json")
    parser.add_argument("--profile", "-p", help="Path to profle directory")
    parser.add_argument(
        "--setting",
        "-s",
        nargs=1,
        action="append",
        default=[],
        help="Override profile setting(s)",
    )
    parser.add_argument(
        "--debug", action="store_true", help="Print DEBUG log to console"
    )

    sub_parsers = parser.add_subparsers()
    sub_parsers.required = True
    sub_parsers.dest = "command"

    print_parser = sub_parsers.add_parser(
        "print-profile", help="Print profile JSON to stdout"
    )
    print_parser.set_defaults(func=print_profile)

    # train-profile
    train_parser = sub_parsers.add_parser(
        "train-profile", help="Train voice2json profile"
    )
    train_parser.set_defaults(func=train)

    # transcribe-wav
    transcribe_parser = sub_parsers.add_parser(
        "transcribe-wav", help="Transcribe WAV file to text"
    )
    transcribe_parser.set_defaults(func=transcribe)
    transcribe_parser.add_argument(
        "--stdin-files",
        "-f",
        action="store_true",
        help="Read WAV file paths from stdin instead of WAV data",
    )
    transcribe_parser.add_argument(
        "--open",
        "-o",
        action="store_true",
        help="Use large pre-built model for transcription",
    )
    transcribe_parser.add_argument(
        "wav_file", nargs="*", default=[], help="Path(s) to WAV file(s)"
    )

    # recognize-intent
    recognize_parser = sub_parsers.add_parser(
        "recognize-intent", help="Recognize intent from JSON or text"
    )
    recognize_parser.set_defaults(func=recognize)
    recognize_parser.add_argument(
        "sentence", nargs="*", default=[], help="Sentences to recognize"
    )
    recognize_parser.add_argument(
        "--text-input",
        "-t",
        action="store_true",
        help="Input is plain text instead of JSON",
    )
    recognize_parser.add_argument(
        "--perplexity",
        "-p",
        action="store_true",
        help="Compute perplexity of input text relative to language model",
    )

    # record-command
    command_parser = sub_parsers.add_parser(
        "record-command", help="Record voice command and write WAV to stdout"
    )
    command_parser.add_argument(
        "--audio-source", "-a", help="File to read raw 16-bit 16Khz mono audio from"
    )
    command_parser.add_argument(
        "--wav-sink", "-w", help="File to write WAV data to instead of stdout"
    )
    command_parser.set_defaults(func=record_command)

    # wait-wake
    wake_parser = sub_parsers.add_parser(
        "wait-wake", help="Listen to audio until wake word is spoken"
    )
    wake_parser.add_argument(
        "--audio-source", "-a", help="File to read raw 16-bit 16Khz mono audio from"
    )
    wake_parser.add_argument(
        "--exit-count",
        "-c",
        type=int,
        help="Exit after the wake word has been spoken some number of times",
    )
    wake_parser.set_defaults(func=wake)

    # pronounce-word
    pronounce_parser = sub_parsers.add_parser(
        "pronounce-word", help="Speak a word phonetically"
    )
    pronounce_parser.add_argument("word", nargs="*", help="Word(s) to prononunce")
    pronounce_parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Don't speak word; only print phonemes",
    )
    pronounce_parser.add_argument(
        "--delay", "-d", type=float, default=0, help="Seconds to wait between words"
    )
    pronounce_parser.add_argument(
        "--nbest",
        "-n",
        type=int,
        default=5,
        help="Number of pronunciations to generate for unknown words",
    )
    pronounce_parser.add_argument("--wav-sink", "-w", help="File to write WAV data to")
    pronounce_parser.set_defaults(func=pronounce)

    # generate-examples
    generate_parser = sub_parsers.add_parser(
        "generate-examples", help="Randomly generate example intents from profile"
    )
    generate_parser.add_argument(
        "--number", "-n", type=int, required=True, help="Number of examples to generate"
    )
    generate_parser.add_argument(
        "--raw-symbols",
        action="store_true",
        help="Output symbols directly from finite state transducer",
    )
    generate_parser.add_argument(
        "--iob", action="store_true", help="Output IOB format instead of JSON"
    )
    generate_parser.set_defaults(func=generate)

    # record-examples
    record_examples_parser = sub_parsers.add_parser(
        "record-examples",
        help="Randomly generate example prompts and have the user record them",
    )
    record_examples_parser.add_argument(
        "--directory",
        "-d",
        help="Directory to save recorded WAV files and transcriptions",
    )
    record_examples_parser.add_argument(
        "--audio-source", "-a", help="File to read raw 16-bit 16Khz mono audio from"
    )
    record_examples_parser.add_argument(
        "--chunk-size",
        type=int,
        default=1024,
        help="Number of bytes to read at a time from stdin",
    )
    record_examples_parser.set_defaults(func=record_examples)

    # test-examples
    test_examples_parser = sub_parsers.add_parser(
        "test-examples", help="Test performance on previously recorded examples"
    )
    test_examples_parser.add_argument(
        "--directory", "-d", help="Directory with recorded examples"
    )
    test_examples_parser.add_argument(
        "--results", "-r", help="Directory to save test results"
    )
    test_examples_parser.add_argument(
        "--open",
        "-o",
        action="store_true",
        help="Use large pre-built model for transcription",
    )
    test_examples_parser.set_defaults(func=test_examples)

    # tune-examples
    tune_examples_parser = sub_parsers.add_parser(
        "tune-examples", help="Tune speech recognizer with previously recorded examples"
    )
    tune_examples_parser.add_argument(
        "--directory", "-d", help="Directory with recorded examples"
    )
    tune_examples_parser.set_defaults(func=tune_examples)

    # show-documentation
    show_documentation_parser = sub_parsers.add_parser(
        "show-documentation", help="Run local HTTP server with documentation"
    )
    show_documentation_parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to host web server on (default: 8000)",
    )
    show_documentation_parser.set_defaults(func=show_documentation)

    # -------------------------------------------------------------------------

    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)

    logger.debug(args)

    profile_yaml: Optional[Path] = None

    if args.profile is None:
        # Guess profile location in $HOME/.config/voice2json
        if "XDG_CONFIG_HOME" in os.environ:
            config_home = Path(os.environ["XDG_CONFIG_HOME"])
        else:
            config_home = Path("~/.config").expanduser()

        profile_dir = config_home / "voice2json"
        logger.debug(f"Assuming profile is at {profile_dir}")

    else:
        # Use profile provided on command line
        profile_dir_or_file = Path(args.profile)
        if profile_dir_or_file.is_dir():
            # Assume directory with profile.yaml
            profile_dir = profile_dir_or_file
        else:
            # Assume YAML file
            profile_dir = profile_dir_or_file.parent
            profile_yaml = profile_dir_or_file

    # Set environment variable usually referenced in profile
    profile_dir = profile_dir.resolve()
    os.environ["profile_dir"] = str(profile_dir)

    # x86_64, armv7l, armv6l, ...
    os.environ["machine"] = platform.machine()

    # Load profile defaults
    defaults_yaml = (
        Path(os.environ.get("voice2json_dir", os.getcwd()))
        / "etc"
        / "profile.defaults.yml"
    )
    if defaults_yaml.exists():
        logger.debug(f"Loading profile defaults from {defaults_yaml}")
        with open(defaults_yaml, "r") as defaults_file:
            profile = yaml.safe_load(defaults_file)
    else:
        # No defaults
        profile = {}

    # Load profile
    if profile_yaml is None:
        profile_yaml = profile_dir / "profile.yml"

    logger.debug(f"Loading profile from {profile_yaml}")

    if profile_yaml.exists():
        with open(profile_yaml, "r") as profile_file:
            recursive_update(profile, yaml.safe_load(profile_file) or {})
    else:
        logger.warning(f"{profile_yaml} does not exist. Using default settings.")

    # Override settings
    for setting_path, setting_value in args.setting:
        setting_value = json.loads(setting_value)
        logger.debug(f"Overring {setting_name} with {setting_value}")
        pydash.set_(profile, setting_path, setting_value)

    # Call sub-commmand
    args.func(args, profile_dir, profile)


# -----------------------------------------------------------------------------


def print_profile(
    args: argparse.Namespace, profile_dir: Path, profile: Dict[str, Any]
) -> None:
    json.dump(profile, sys.stdout, indent=4)


# -----------------------------------------------------------------------------


def train(args: argparse.Namespace, profile_dir: Path, profile: Dict[str, Any]) -> None:
    from voice2json.train import train_profile

    # Strip voice2json command-line arguments so doit won't pick them up
    sys.argv = [sys.argv[0]]

    train_profile(profile_dir, profile)


# -----------------------------------------------------------------------------


def transcribe(
    args: argparse.Namespace, profile_dir: Path, profile: Dict[str, Any]
) -> None:
    from voice2json import get_transcriber

    # Make sure profile has been trained
    check_trained(profile, profile_dir)

    transcriber = get_transcriber(
        profile_dir, profile, open_transcription=args.open, debug=args.debug
    )

    try:
        if (len(args.wav_file) > 0) or args.stdin_files:
            # Read WAV file paths
            wav_files = args.wav_file
            if args.stdin_files:
                logger.debug("Reading file paths from stdin")
                wav_files = itertools.chain(wav_files, sys.stdin)

            for wav_path_str in wav_files:
                wav_path_str = wav_path_str.strip()

                # Load and convert
                wav_path = Path(wav_path_str)
                logger.debug(f"Transcribing {wav_path}")

                wav_data = wav_path.read_bytes()

                # Transcribe
                result = transcriber.transcribe_wav(wav_data)

                # Add name of WAV file to result
                result["wav_name"] = wav_path.name

                print_json(result)
        else:
            # Read WAV data from stdin
            logger.debug("Reading WAV data from stdin")

            # Load and convert
            wav_data = sys.stdin.buffer.read()

            # Transcribe
            result = transcriber.transcribe_wav(wav_data)
            print_json(result)
    finally:
        transcriber.stop()


# -----------------------------------------------------------------------------


def recognize(
    args: argparse.Namespace, profile_dir: Path, profile: Dict[str, Any]
) -> None:
    from voice2json import get_recognizer

    # Make sure profile has been trained
    check_trained(profile, profile_dir)

    # Load settings
    intent_fst_path = ppath(
        profile, profile_dir, "intent-recognition.intent-fst", "intent.fst"
    )

    word_casing = pydash.get(profile, "training.word-casing", "ignore").lower()

    # Load intent recognizer
    recognizer = get_recognizer(profile_dir, profile)

    if len(args.sentence) > 0:
        sentences = args.sentence
    else:
        logger.debug("Reading sentences from stdin")
        sentences = sys.stdin

    # Process sentences
    for sentence in sentences:
        if args.text_input:
            # Input is plain text
            text = sentence
            sentence_object = {"text": text}
        else:
            # Input is JSON
            sentence_object = json.loads(sentence)
            text = sentence_object.get("text", "")

        text = text.strip()

        if word_casing == "upper":
            text = text.upper()
        elif word_casing == "lower":
            text = text.lower()

        intent = recognizer.recognize(text)

        if args.perplexity:
            try:
                # Compute sentence perplexity with ngramperplexity
                with tempfile.TemporaryDirectory() as temp_dir_name:
                    temp_path = Path(temp_dir_name)
                    text_path = temp_path / "sentence.txt"
                    text_path.write_text(text)

                    symbols_path = temp_path / "sentence.syms"
                    subprocess.check_call(
                        ["ngramsymbols", str(text_path), str(symbols_path)]
                    )

                    far_path = temp_path / "sentence.far"
                    subprocess.check_call(
                        [
                            "farcompilestrings",
                            f"-symbols={symbols_path}",
                            "-keep_symbols=1",
                            str(text_path),
                            str(far_path),
                        ]
                    )

                    output = subprocess.check_output(
                        [
                            "ngramperplexity",
                            str(profile_dir / "intent.fst.model"),
                            str(far_path),
                        ]
                    ).decode()

                    last_line = output.strip().splitlines()[-1]
                    perplexity = float(
                        re.match(r"^.*perplexity\s*=\s*(.+)$", last_line).group(1)
                    )
                    intent["perplexity"] = perplexity
            except Exception as e:
                logger.exception("perplexity")

        # Merge with input object
        for key, value in intent.items():
            sentence_object[key] = value

        print_json(sentence_object)


# -----------------------------------------------------------------------------


def record_command(
    args: argparse.Namespace, profile_dir: Path, profile: Dict[str, Any]
) -> None:
    from voice2json.command.webrtcvad import wait_for_command
    from voice2json.utils import buffer_to_wav, get_audio_source

    # Make sure profile has been trained
    check_trained(profile, profile_dir)

    # Load settings
    vad_mode = int(pydash.get(profile, "voice-command.vad-mode", 3))
    min_seconds = float(pydash.get(profile, "voice-command.minimum-seconds", 2))
    max_seconds = float(pydash.get(profile, "voice-command.maximum-seconds", 30))
    speech_seconds = float(pydash.get(profile, "voice-command.speech-seconds", 0.3))
    silence_seconds = float(pydash.get(profile, "voice-command.silence-seconds", 0.5))
    before_seconds = float(pydash.get(profile, "voice-command.before-seconds", 0.25))

    # Expecting raw 16-bit, 16Khz mono audio
    if args.audio_source is None:
        audio_source = get_audio_source(profile)
        logger.debug(f"Recording raw 16-bit 16Khz mono audio")
    elif args.audio_source == "-":
        audio_source = sys.stdin.buffer
        logger.debug(f"Recording raw 16-bit 16Khz mono audio from stdin")
    else:
        audio_source: BinaryIO = open(args.audio_source, "rb")
        logger.debug(f"Recording raw 16-bit 16Khz mono audio from {args.audio_source}")

    # JSON events are not printed by default
    json_file = None
    wav_sink = sys.stdout.buffer

    if (args.wav_sink is not None) and (args.wav_sink != "-"):
        wav_sink = open(args.wav_sink, "wb")

        # Print JSON to stdout
        json_file = sys.stdout

    # Record command
    audio_buffer = wait_for_command(
        audio_source,
        vad_mode=vad_mode,
        min_seconds=min_seconds,
        max_seconds=max_seconds,
        speech_seconds=speech_seconds,
        silence_seconds=silence_seconds,
        before_seconds=before_seconds,
        json_file=json_file,
    )

    # Output WAV data
    wav_bytes = buffer_to_wav(audio_buffer)
    wav_sink.write(wav_bytes)


# -----------------------------------------------------------------------------


def wake(args: argparse.Namespace, profile_dir: Path, profile: Dict[str, Any]) -> None:
    import struct
    from voice2json.utils import get_audio_source
    from voice2json.wake.porcupine import Porcupine

    # Load settings
    library_path = ppath(profile, profile_dir, "wake-word.porcupine.library-file")
    params_path = ppath(profile, profile_dir, "wake-word.porcupine.params-file")
    keyword_path = ppath(profile, profile_dir, "wake-word.porcupine.keyword-file")
    sensitivity = float(pydash.get(profile, "wake-word.sensitivity", 0.5))

    # Load porcupine
    handle = Porcupine(
        str(library_path),
        str(params_path),
        keyword_file_paths=[str(keyword_path)],
        sensitivities=[sensitivity],
    )

    chunk_size = handle.frame_length * 2
    chunk_format = "h" * handle.frame_length

    # Expecting raw 16-bit, 16Khz mono audio
    if args.audio_source is None:
        audio_source = get_audio_source(profile)
        logger.debug(f"Recording raw 16-bit 16Khz mono audio")
    elif args.audio_source == "-":
        audio_source = sys.stdin.buffer
        logger.debug(f"Recording raw 16-bit 16Khz mono audio from stdin")
    else:
        audio_source: BinaryIO = open(args.audio_source, "rb")
        logger.debug(f"Recording raw 16-bit 16Khz mono audio from {args.audio_source}")

    # Read first audio chunk
    start_time = time.time()
    chunk = audio_source.read(chunk_size)

    try:
        while len(chunk) == chunk_size:
            # Process audio chunk
            unpacked_chunk = struct.unpack_from(chunk_format, chunk)
            keyword_index = handle.process(unpacked_chunk)

            if keyword_index:
                result = {
                    "keyword": str(keyword_path),
                    "detect_seconds": time.time() - start_time,
                }
                print_json(result)

                # Check exit count
                if args.exit_count is not None:
                    args.exit_count -= 1
                    if args.exit_count <= 0:
                        break

            # Get next chunk
            chunk = audio_source.read(chunk_size)
    except KeyboardInterrupt:
        pass  # expected


# -----------------------------------------------------------------------------


def pronounce(
    args: argparse.Namespace, profile_dir: Path, profile: Dict[str, Any]
) -> None:
    from voice2json.utils import read_dict

    base_dictionary_path = ppath(
        profile, profile_dir, "training.base_dictionary", "base_dictionary.txt"
    )
    dictionary_path = ppath(
        profile, profile_dir, "training.dictionary", "dictionary.txt"
    )
    g2p_path = ppath(profile, profile_dir, "training.g2p-model", "g2p.fst")
    g2p_exists = g2p_path.exists()

    map_path = ppath(
        profile, profile_dir, "text-to-speech.espeak.phoneme-map", "espeak_phonemes.txt"
    )
    map_exists = map_path.exists()
    espeak_cmd_format = pydash.get(
        profile,
        "text-to-speech.espeak.pronounce-command",
        "espeak-ng -s 80 [[{phonemes}]]",
    )

    word_casing = pydash.get(profile, "training.word-casing", "ignore").lower()

    # True if audio will go to stdout.
    # In this case, printing will go to stderr.
    wav_stdout = args.wav_sink == "-"

    print_file = sys.stderr if wav_stdout else sys.stdout

    # Load dictionaries
    pronunciations: Dict[str, List[str]] = defaultdict(list)

    for dict_path in [dictionary_path, base_dictionary_path]:
        if dict_path.exists():
            with open(dict_path, "r") as dict_file:
                read_dict(dict_file, pronunciations)

    if len(args.word) > 0:
        words = args.word
    else:
        words = sys.stdin

    # Process words
    for word in words:
        word_parts = re.split(r"\s+", word.strip())
        word = word_parts[0]
        dict_phonemes = []

        if word_casing == "upper":
            word = word.upper()
        elif word_casing == "lower":
            word = word.lower()

        if len(word_parts) > 1:
            # Pronunciation provided
            dict_phonemes.append(word_parts[1:])

        if word in pronunciations:
            # Use pronunciations from dictionary
            dict_phonemes.extend(re.split(r"\s+", p) for p in pronunciations[word])
        elif g2p_exists:
            # Don't guess if a pronunciation was provided
            if len(dict_phonemes) == 0:
                # Guess pronunciation with phonetisaurus
                logger.debug(f"Guessing pronunciation for {word}")

                with tempfile.NamedTemporaryFile(mode="w") as word_file:
                    print(word, file=word_file)
                    word_file.seek(0)

                    phonetisaurus_cmd = [
                        "phonetisaurus-apply",
                        "--model",
                        str(g2p_path),
                        "--word_list",
                        word_file.name,
                        "--nbest",
                        str(args.nbest),
                    ]

                    logger.debug(phonetisaurus_cmd)
                    output_lines = (
                        subprocess.check_output(phonetisaurus_cmd).decode().splitlines()
                    )
                    dict_phonemes.extend(
                        re.split(r"\s+", line.strip())[1:] for line in output_lines
                    )
        else:
            logger.warn(f"No pronunciation for {word}")

        # Avoid duplicate pronunciations
        used_pronunciations: Set[str] = set()

        for phonemes in dict_phonemes:
            phoneme_str = " ".join(phonemes)
            if phoneme_str in used_pronunciations:
                continue

            print(word, phoneme_str, file=print_file)
            used_pronunciations.add(phoneme_str)

            if not args.quiet:
                if map_exists:
                    # Map to espeak phonemes
                    phoneme_map = dict(
                        re.split(r"\s+", line.strip(), maxsplit=1)
                        for line in map_path.read_text().splitlines()
                    )

                    espeak_phonemes = [phoneme_map[p] for p in phonemes]
                else:
                    espeak_phonemes = phonemes

                # Speak with espeak
                espeak_str = "".join(espeak_phonemes)
                espeak_cmd = shlex.split(espeak_cmd_format.format(phonemes=espeak_str))

                # Determine where to output WAV data
                if args.wav_sink is not None:
                    if args.wav_sink == "-":
                        # stdout
                        espeak_cmd.append("--stdout")
                    else:
                        # File output
                        espeak_cmd.extend(["-w", args.wav_sink])

                logger.debug(espeak_cmd)
                subprocess.check_call(espeak_cmd)

                time.sleep(args.delay)


# -----------------------------------------------------------------------------


def generate(
    args: argparse.Namespace, profile_dir: Path, profile: Dict[str, Any]
) -> None:
    import pywrapfst as fst
    from voice2json.train.jsgf2fst import fstprintall, symbols2intent

    # Make sure profile has been trained
    check_trained(profile, profile_dir)

    # Load settings
    intent_fst_path = ppath(
        profile, profile_dir, "intent-recognition.intent-fst", "intent.fst"
    )

    # Load intent finite state transducer
    intent_fst = fst.Fst.read(str(intent_fst_path))

    if args.number <= 0:
        # Generatel all possible examples
        rand_fst = intent_fst
    else:
        # Generate samples
        rand_fst = fst.randgen(intent_fst, npath=args.number)

    # Convert to words/tokens
    for symbols in fstprintall(rand_fst, exclude_meta=False):
        if args.raw_symbols:
            print(" ".join(symbols))
            continue

        # Convert to intent
        intent = symbols2intent(symbols)

        # Add slots
        intent["slots"] = {}
        for ev in intent["entities"]:
            intent["slots"][ev["entity"]] = ev["value"]

        if args.iob:
            # IOB format
            token_idx = 0
            entity_start = {ev["start"]: ev for ev in intent["entities"]}
            entity_end = {ev["end"]: ev for ev in intent["entities"]}
            entity = None

            word_tags = []
            for word in intent["tokens"]:
                # Determine tag label
                tag = "O" if not entity else f"I-{entity}"
                if token_idx in entity_start:
                    entity = entity_start[token_idx]["entity"]
                    tag = f"B-{entity}"

                word_tags.append((word, tag))

                # word ner
                token_idx += len(word) + 1

                if (token_idx - 1) in entity_end:
                    entity = None

            print("BS", end=" ")
            for wt in word_tags:
                print(wt[0], end=" ")
            print("ES", end="\t")

            print("O", end=" ")  # BS
            for wt in word_tags:
                print(wt[1], end=" ")
            print("O", end="\t")  # ES

            # Intent name last
            print(intent["intent"]["name"])
        else:
            # Write as jsonl
            print_json(intent)


# -----------------------------------------------------------------------------


def record_examples(
    args: argparse.Namespace, profile_dir: Path, profile: Dict[str, Any]
) -> None:
    import pywrapfst as fst
    from voice2json.utils import buffer_to_wav, get_audio_source
    from voice2json.train.jsgf2fst import fstprintall, symbols2intent

    # Make sure profile has been trained
    check_trained(profile, profile_dir)

    chunk_size = args.chunk_size
    examples_dir = Path(args.directory) if args.directory is not None else Path.cwd()
    examples_dir.mkdir(parents=True, exist_ok=True)

    # Load settings
    intent_fst_path = ppath(
        profile, profile_dir, "intent-recognition.intent-fst", "intent.fst"
    )

    # Load intent finite state transducer
    intent_fst = fst.Fst.read(str(intent_fst_path))

    def generate_intent() -> Dict[str, Any]:
        # Generate sample sentence
        rand_fst = fst.randgen(intent_fst, npath=1)

        # Convert to words/tokens
        symbols = fstprintall(rand_fst, exclude_meta=False)[0]

        # Convert to intent
        return symbols2intent(symbols)

    def get_wav_path(text: str, count: int) -> Path:
        # /dir/the_transcription_text-000.wav
        text = re.sub(r"\s+", "_", text)
        return examples_dir / f"{text}-{count:03d}.wav"

    # Expecting raw 16-bit, 16Khz mono audio
    if args.audio_source is None:
        audio_source = get_audio_source(profile)
        logger.debug(f"Recording raw 16-bit 16Khz mono audio")
    elif args.audio_source == "-":
        audio_source = sys.stdin.buffer
        logger.debug(f"Recording raw 16-bit 16Khz mono audio from stdin")
    else:
        audio_source: BinaryIO = open(args.audio_source, "rb")
        logger.debug(f"Recording raw 16-bit 16Khz mono audio from {args.audio_source}")

    # Recording thread
    audio_data = bytes()
    recording = False

    def record_audio():
        nonlocal audio_source, recording, audio_data
        while audio_source is not None:
            chunk = audio_source.read(chunk_size)
            if recording:
                audio_data += chunk

    record_thread = threading.Thread(target=record_audio, daemon=True)
    record_thread.start()

    try:
        while True:
            # Generate random intent for prompt
            random_intent = generate_intent()
            text = random_intent["text"]

            # Prompt
            print("---")
            print(text)

            # Instructions
            print("Press ENTER to start recording (CTRL+C to exit)")
            input()

            # Record
            audio_data = bytes()
            recording = True

            # Instructions
            print("Recording from audio source. Press ENTER to stop (CTRL+C to exit).")
            input()

            # Save WAV
            recording = False
            logging.debug(f"Recorded {len(audio_data)} byte(s) of audio data")

            count = 0
            wav_path = get_wav_path(text, count)
            while wav_path.exists():
                # Find unique name
                count += 1
                wav_path = get_wav_path(text, count)

            wav_bytes = buffer_to_wav(audio_data)
            wav_path.write_bytes(wav_bytes)

            # Save transcription
            transcript_path = examples_dir / f"{wav_path.stem}.txt"
            transcript_path.write_text(text)

            # Save intent
            intent_path = examples_dir / f"{wav_path.stem}.json"
            with open(intent_path, "w") as intent_file:
                with jsonlines.Writer(intent_file) as out:
                    out.write(random_intent)

            # Response
            print("Wrote", wav_path)
            print("")

    except KeyboardInterrupt:
        audio_source = None
        record_thread.join()


# -----------------------------------------------------------------------------


def test_examples(
    args: argparse.Namespace, profile_dir: Path, profile: Dict[str, Any]
) -> None:
    from voice2json import get_transcriber, get_recognizer

    # Make sure profile has been trained
    check_trained(profile, profile_dir)

    examples_dir = Path(args.directory) if args.directory is not None else Path.cwd()
    logger.debug(f"Looking for examples in {examples_dir}")

    results_dir = None
    if args.results is not None:
        results_dir = Path(args.results)

    # Load WAV transcriber
    transcriber = get_transcriber(
        profile_dir, profile, debug=args.debug, open_transcription=args.open
    )

    # Optional intent recognizer
    recognizer = None

    # ----------
    # Statistics
    # ----------

    # Total number of WAV files
    num_wavs = 0

    # Number transcriptions that match *exactly*
    correct_transcriptions = 0

    # Number of words in all transcriptions (as counted by word_align.pl)
    num_words = 0

    # Number of correct words in all transcriptions (as computed by word_align.pl)
    correct_words = 0

    # Total number of intents that were attempted
    num_intents = 0

    # Number of recognized intents that match expectations
    correct_intent_names = 0

    # Number of entity/value pairs that match *exactly* in all recognized intents
    correct_entities = 0

    # Number of entity/value pairs all intents
    num_entities = 0

    # Expected/actual intents
    expected: Dict[str, Dict[str, Any]] = {}
    actual: Dict[str, Dict[str, Any]] = {}

    # Process examples
    try:
        for wav_path in examples_dir.glob("*.wav"):
            logger.debug(f"Processing {wav_path}")

            # Load expected transcription
            transcript_path = examples_dir / f"{wav_path.stem}.txt"
            expected_text = None

            if transcript_path.exists():
                expected_text = transcript_path.read_text().strip()

            # Load expected intent (optional)
            intent_path = examples_dir / f"{wav_path.stem}.json"
            expected_intent = None
            if intent_path.exists():
                with open(intent_path, "r") as intent_file:
                    expected_intent = json.load(intent_file)

                # Use full intent
                expected[wav_path.name] = expected_intent
            else:
                # Use text only
                expected[wav_path.name] = {"text": expected_text}

            if (expected_text is None) and (expected_intent is None):
                logger.warn(f"Skipping {wav_path} (no transcription or intent files)")
                continue
            elif expected_text is None:
                # Use text from intent
                expected_text = expected_intent["text"]

            # Transcribe WAV
            wav_data = wav_path.read_bytes()
            actual_transcription = transcriber.transcribe_wav(wav_data)
            actual_text = actual_transcription.get(
                "raw_text", actual_transcription["text"]
            )
            logger.debug(actual_text)

            if expected_text == actual_text:
                correct_transcriptions += 1

            # Do recognition
            if expected_intent is not None:
                if recognizer is None:
                    # Load recognizer on demand
                    recognizer = get_recognizer(profile_dir, profile)

                num_intents += 1
                actual_intent = recognizer.recognize(actual_text)

                # Merge with transcription
                for key, value in actual_transcription.items():
                    if key not in actual_intent:
                        actual_intent[key] = value

                logger.debug(actual_intent)
                intents_match = (
                    expected_intent["intent"]["name"] == actual_intent["intent"]["name"]
                )

                # Count entities
                expected_entities: List[Tuple[str, str]] = []
                for entity_dict in expected_intent.get("entities", []):
                    num_entities += 1
                    entity_tuple = (entity_dict["entity"], entity_dict["value"])
                    expected_entities.append(entity_tuple)

                # Verify actual entities.
                # Only check entities if intent was correct.
                if intents_match:
                    correct_intent_names += 1
                    for entity_dict in actual_intent.get("entities", []):
                        entity_tuple = (entity_dict["entity"], entity_dict["value"])

                        if entity_tuple in expected_entities:
                            correct_entities += 1
                            expected_entities.remove(entity_tuple)

                # Record full intent
                actual[wav_path.name] = actual_intent
            else:
                # Record transcription result only
                actual[wav_path.name] = actual_transcription

            num_wavs += 1

        # ---------------------------------------------------------------------

        if num_wavs < 1:
            logger.fatal("No WAV files found")
            sys.exit(1)

        # Compute word error rate (WER)
        align_results: Dict[str, Any] = {}
        if shutil.which("word_align.pl"):
            from voice2json.utils import align2json

            with tempfile.NamedTemporaryFile(mode="w") as reference_file:
                # Write references
                for expected_key, expected_intent in expected.items():
                    print(
                        expected_intent.get("raw_text", expected_intent["text"]),
                        f"({expected_key})",
                        file=reference_file,
                    )

                with tempfile.NamedTemporaryFile(mode="w") as hypothesis_file:
                    # Write hypotheses
                    for actual_key, actual_intent in actual.items():
                        print(
                            actual_intent.get("raw_text", actual_intent["text"]),
                            f"({actual_key})",
                            file=hypothesis_file,
                        )

                    # Calculate WER
                    reference_file.seek(0)
                    hypothesis_file.seek(0)

                    align_cmd = [
                        "word_align.pl",
                        reference_file.name,
                        hypothesis_file.name,
                    ]
                    logger.debug(align_cmd)

                    align_output = subprocess.check_output(align_cmd).decode()
                    if results_dir is not None:
                        align_output_path = results_dir / "word_align.txt"
                        align_output_path.write_text(align_output)
                        logger.debug(f"Wrote {align_output_path}")

                    # Convert to JSON
                    with io.StringIO(align_output) as align_file:
                        align_results = align2json(align_file)

        else:
            logger.warn("word_align.pl not found in PATH. Not computing WER.")

        # Merge WER results
        for key, wer in align_results.items():
            actual[key]["word_error"] = wer
            num_words += wer["words"]
            correct_words += wer["correct"]

        # Summarize results
        summary = {
            "statistics": {
                "num_wavs": num_wavs,
                "num_words": num_words,
                "num_entities": num_entities,
                "correct_transcriptions": correct_transcriptions,
                "correct_intent_names": correct_intent_names,
                "correct_words": correct_words,
                "correct_entities": correct_entities,
                "transcription_accuracy": correct_words / num_words
                if num_words > 0
                else 1,
                "intent_accuracy": correct_intent_names / num_intents
                if num_intents > 0
                else 1,
                "entity_accuracy": correct_entities / num_entities
                if num_entities > 0
                else 1,
            },
            "actual": actual,
            "expected": expected,
        }

        print_json(summary)

    finally:
        transcriber.stop()


# -----------------------------------------------------------------------------


def tune_examples(
    args: argparse.Namespace, profile_dir: Path, profile: Dict[str, Any]
) -> None:
    from voice2json import get_tuner

    examples_dir = Path(args.directory) if args.directory is not None else Path.cwd()
    logger.debug(f"Looking for examples in {examples_dir}")

    start_time = time.time()

    tuner = get_tuner(profile_dir, profile)
    tuner.tune(examples_dir)

    end_time = time.time()
    print("Tuning completed in", end_time - start_time, "second(s)")


# -----------------------------------------------------------------------------


def show_documentation(
    args: argparse.Namespace, profile_dir: Path, profile: Dict[str, Any]
) -> None:
    import http.server
    import socketserver

    voice2json_dir = Path(os.environ.get("voice2json_dir", os.getcwd()))
    site_dir = voice2json_dir / "site"

    os.chdir(site_dir)
    Handler = http.server.SimpleHTTPRequestHandler
    httpd = socketserver.TCPServer(("", args.port), Handler)
    print(f"Running HTTP server at http://127.0.0.1:{args.port}")
    httpd.serve_forever()


# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------


def print_json(value: Any) -> None:
    """Prints a single line of JSON to stdout."""
    with jsonlines.Writer(sys.stdout) as out:
        out.write(value)

    sys.stdout.flush()


def env_constructor(loader, node):
    """Expands !env STRING to replace environment variables in STRING."""
    return os.path.expandvars(node.value)


yaml.SafeLoader.add_constructor("!env", env_constructor)


def check_trained(profile: Dict[str, Any], profile_dir: Path) -> None:
    """Checks important files to see if profile is not trained. Exits if it isn't."""
    # Load settings
    dictionary_path = ppath(
        profile, profile_dir, "speech-to-text.dictionary", "dictionary.txt"
    )

    language_model_path = ppath(
        profile, profile_dir, "speech-to-text.language-model", "language_model.txt"
    )

    intent_fst_path = ppath(
        profile, profile_dir, "intent-recognition.intent-fst", "intent.fst"
    )

    missing = False
    for path in [dictionary_path, language_model_path, intent_fst_path]:
        if not path.exists():
            logger.fatal(f"Missing {path}. Did you forget to run train-profile?")
            missing = True

    if missing:
        sys.exit(1)


# -----------------------------------------------------------------------------


if __name__ == "__main__":
    main()
