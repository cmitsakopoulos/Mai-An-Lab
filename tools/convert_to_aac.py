#!/usr/bin/env python3
"""
Convert audio files to AAC (highest quality/bitrate) using FFmpeg.
Supports Apple AudioToolbox (aac_at) on macOS and falls back to native FFmpeg aac.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

def check_ffmpeg():
    """Verify ffmpeg is installed and detect supported encoders."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True
        )
        encoders = result.stdout
        has_aac_at = "aac_at" in encoders
        has_aac = "aac" in encoders
        return True, has_aac_at, has_aac
    except (subprocess.SubprocessError, FileNotFoundError):
        return False, False, False

def get_sample_rate(input_path: Path) -> int:
    """Query the sample rate of the input file using ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=sample_rate", "-of", "default=noprint_wrappers=1:nokey=1", str(input_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True
        )
        return int(result.stdout.strip())
    except Exception:
        return 44100  # Default fallback

def convert_file(input_path: Path, output_dir: Path, encoder: str, use_vbr: bool, bitrate: str, overwrite: bool):
    """Convert a single audio file to AAC."""
    if not input_path.exists():
        print(f"Error: Input file does not exist: {input_path}")
        return False

    # Define output path (AAC files standard container is .m4a)
    output_path = output_dir / f"{input_path.stem}.m4a"

    if output_path.exists() and not overwrite:
        print(f"Skipping: {output_path.name} (already exists). Use --overwrite to force.")
        return True

    # Check sample rate
    sample_rate = get_sample_rate(input_path)

    # Build ffmpeg command
    cmd = [
        "ffmpeg",
        "-y" if overwrite else "-n",
        "-i", str(input_path),
        "-c:a", encoder,
    ]

    # Limit sample rate if necessary for AAC/AudioToolbox encoder
    if sample_rate > 48000:
        print(f"Note: Downsampling {input_path.name} from {sample_rate}Hz to 48000Hz for AAC compatibility.")
        cmd.extend(["-ar", "48000"])

    if encoder == "aac_at":
        if use_vbr:
            # Highest quality VBR settings for AudioToolbox AAC
            cmd.extend([
                "-aac_at_mode", "3",       # 3 = Variable Bitrate (VBR)
                "-q:a", "0",               # 0 = Highest VBR quality level mapped in FFmpeg
                "-aac_at_quality", "0"     # 0 = Highest encoder effort/quality setting
            ])
        else:
            # CBR highest bitrate
            cmd.extend([
                "-b:a", bitrate
            ])
    else:  # native aac
        if use_vbr:
            # Native aac VBR: -q:a 2 is high quality (approx 192k), but for maximum quality
            # we default to CBR at 320k unless user explicitly requested a specific quality level.
            # Thus, we fall back to CBR 320k for highest bitrate by default.
            cmd.extend([
                "-b:a", bitrate
            ])
        else:
            cmd.extend([
                "-b:a", bitrate
            ])

    # Map audio and optional video stream (cover art), copying video codec directly
    cmd.extend([
        "-map", "0:a",
        "-map", "0:v?",
        "-c:v", "copy",
        "-map_metadata", "0",
        str(output_path)
    ])

    print(f"Converting: {input_path.name} -> {output_path.name}")
    
    try:
        # Run conversion, capture stderr/stdout for error reporting
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        if result.returncode != 0:
            print(f"Failed to convert {input_path.name}:")
            print(result.stderr)
            return False
        
        orig_size = input_path.stat().st_size / (1024 * 1024)
        new_size = output_path.stat().st_size / (1024 * 1024)
        print(f"Successfully converted. Size: {orig_size:.2f} MB -> {new_size:.2f} MB ({new_size/orig_size:.1%})")
        return True
    except Exception as e:
        print(f"Exception converting {input_path.name}: {e}")
        return False

def main():
    parser = argparse.ArgumentParser(description="Convert audio files to AAC (M4A) using FFmpeg.")
    parser.add_argument("inputs", nargs="+", help="Input files or directories to convert.")
    parser.add_argument("-o", "--output-dir", help="Output directory (defaults to same directory as input files).")
    parser.add_argument("-b", "--bitrate", default="320k", help="Bitrate for CBR mode (default: 320k).")
    parser.add_argument("--cbr", action="store_true", help="Force CBR mode (Constant Bitrate). Default is VBR with aac_at.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing output files.")
    parser.add_argument("--encoder", choices=["aac_at", "aac"], help="Force specific AAC encoder.")

    args = parser.parse_args()

    # Verify ffmpeg
    ffmpeg_ok, has_aac_at, has_aac = check_ffmpeg()
    if not ffmpeg_ok:
        print("Error: FFmpeg was not found on your system. Please install it first.", file=sys.stderr)
        sys.exit(1)

    # Determine encoder
    if args.encoder:
        encoder = args.encoder
        if encoder == "aac_at" and not has_aac_at:
            print("Warning: Forced aac_at but it's not supported by this FFmpeg. Falling back to native aac.")
            encoder = "aac"
    else:
        encoder = "aac_at" if has_aac_at else "aac"

    # Default to VBR if using aac_at, unless CBR is forced
    use_vbr = True
    if args.cbr or encoder == "aac":
        use_vbr = False

    print("=" * 60)
    print(f"Audio Converter Tool (using FFmpeg)")
    print(f"Selected Encoder: {encoder}")
    if encoder == "aac_at" and use_vbr:
        print("Mode: VBR (Variable Bitrate) at highest quality (-q:a 0 -aac_at_quality 0)")
    else:
        print(f"Mode: CBR (Constant Bitrate) at {args.bitrate}")
    print("=" * 60)

    # Process inputs
    files_to_convert = []
    for input_str in args.inputs:
        path = Path(input_str)
        if path.is_dir():
            for f in path.glob("*"):
                if f.suffix.lower() in [".flac", ".wav", ".alac", ".aif", ".aiff"]:
                    files_to_convert.append(f)
        elif path.is_file():
            files_to_convert.append(path)
        else:
            print(f"Warning: Input not found: {input_str}")

    if not files_to_convert:
        print("No files to convert. Exiting.")
        sys.exit(0)

    success_count = 0
    for file_path in files_to_convert:
        # Determine output directory
        out_dir = Path(args.output_dir) if args.output_dir else file_path.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        
        if convert_file(file_path, out_dir, encoder, use_vbr, args.bitrate, args.overwrite):
            success_count += 1

    print("=" * 60)
    print(f"Finished: {success_count} / {len(files_to_convert)} files converted successfully.")
    print("=" * 60)

if __name__ == "__main__":
    main()
