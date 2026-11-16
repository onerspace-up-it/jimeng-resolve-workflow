#!/usr/bin/env python3
"""Inspect or prepare generated clips as 4K ProRes media for DaVinci Resolve.

Requires Python 3.9+ plus ffmpeg/ffprobe. Scaling resamples pixels; it is not AI
super-resolution. The default frame policy repeats/drops frames, not optical flow.
"""

import argparse
import datetime
import fractions
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


class MediaError(Exception):
    pass


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def rate(value):
    try:
        return float(fractions.Fraction(str(value)))
    except (ValueError, ZeroDivisionError, TypeError):
        return None


def command(args, description):
    completed = subprocess.run(args, capture_output=True, text=True)
    if completed.returncode:
        details = completed.stderr.strip()[-5000:]
        raise MediaError("{} failed (exit {}): {}".format(
            description, completed.returncode, details or "no diagnostic output"))
    return completed.stdout


def executable(name):
    result = shutil.which(name)
    if not result:
        raise MediaError("Executable not found: {}. Supply its path or install it separately.".format(name))
    return result


def probe(path, ffprobe):
    raw = command([ffprobe, "-v", "error", "-show_format", "-show_streams",
                   "-of", "json", str(path)], "ffprobe")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise MediaError("ffprobe returned invalid JSON: {}".format(error))


def rotation(stream):
    for item in stream.get("side_data_list", []):
        if "rotation" in item:
            return number(item["rotation"]) or 0.0
    return number(stream.get("tags", {}).get("rotate")) or 0.0


def hdr_reasons(stream):
    reasons = []
    transfer = stream.get("color_transfer", "unknown")
    if transfer in ("smpte2084", "arib-std-b67"):
        reasons.append("HDR transfer function: {}".format(transfer))
    for item in stream.get("side_data_list", []):
        kind = item.get("side_data_type", "")
        if any(word in kind.lower() for word in ("mastering display", "content light", "dovi", "dolby vision")):
            reasons.append("HDR-related side data: {}".format(kind))
    return reasons


def video_stream(metadata):
    streams = [s for s in metadata.get("streams", [])
               if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    if not streams:
        raise MediaError("Input has no playable video stream.")
    return streams[0]


def duration(metadata, stream=None):
    return number((stream or {}).get("duration")) or number(metadata.get("format", {}).get("duration"))


def describe(path, metadata):
    selected = video_stream(metadata)
    descriptions = []
    for stream in metadata.get("streams", []):
        item = {key: stream[key] for key in (
            "index", "codec_type", "codec_name", "profile", "width", "height",
            "pix_fmt", "sample_aspect_ratio", "display_aspect_ratio", "avg_frame_rate",
            "r_frame_rate", "duration", "start_time", "nb_frames", "sample_rate",
            "channels", "channel_layout", "color_range", "color_space",
            "color_transfer", "color_primaries") if key in stream}
        if stream.get("codec_type") == "video":
            item["rotation_degrees"] = rotation(stream)
            item["hdr_reasons"] = hdr_reasons(stream)
        descriptions.append(item)
    sar = rate(selected.get("sample_aspect_ratio", "1:1").replace(":", "/")) or 1.0
    w, h = selected.get("width", 0) * sar, selected.get("height", 0)
    if abs(rotation(selected)) % 180 == 90:
        w, h = h, w
    return {
        "path": str(path), "duration_seconds": duration(metadata),
        "selected_video_stream_index": selected["index"],
        "display_dimensions_before_fitting": {"width": w, "height": h},
        "source_cadence": {
            "nominal_rate": selected.get("r_frame_rate"),
            "average_rate": selected.get("avg_frame_rate"),
            "nominal_fps": rate(selected.get("r_frame_rate")),
            "average_fps": rate(selected.get("avg_frame_rate")),
            "variable_frame_rate": "not_determined",
            "note": "Rates are stream metadata; input frame timestamps were not scanned. Different rates alone do not prove VFR."
        },
        "unsupported_hdr": bool(hdr_reasons(selected)),
        "streams": descriptions
    }


def validate(output, source, width, height, fps, ffmpeg, ffprobe):
    metadata = probe(output, ffprobe)
    video = video_stream(metadata)
    if (video.get("width"), video.get("height")) != (width, height):
        raise MediaError("Output dimensions do not match the requested 4K canvas.")
    if abs(rotation(video)) % 360 > 0.01:
        raise MediaError("Output unexpectedly retains a rotation transform.")
    if video.get("codec_name") != "prores" or video.get("pix_fmt") != "yuv422p10le":
        raise MediaError("Output is not 10-bit 4:2:2 ProRes.")
    if abs((rate(video.get("avg_frame_rate")) or 0) - fps) > 0.0001:
        raise MediaError("Output frame-rate metadata does not match the requested rate.")
    source_video = video_stream(source)
    tolerance = max(0.05, 1.5 / fps)
    before, after = duration(source, source_video), duration(metadata, video)
    if before is not None and (after is None or abs(before - after) > tolerance):
        raise MediaError("Video duration changed beyond {:.3f}s tolerance ({} -> {}).".format(tolerance, before, after))
    audio_before = [s for s in source.get("streams", []) if s.get("codec_type") == "audio"]
    audio_after = [s for s in metadata.get("streams", []) if s.get("codec_type") == "audio"]
    if len(audio_before) != len(audio_after):
        raise MediaError("Output audio-track count changed.")
    for old, new in zip(audio_before, audio_after):
        if new.get("codec_name") != "pcm_s16le" or new.get("sample_rate") != "48000":
            raise MediaError("Output audio must be 48 kHz PCM.")
        if old.get("channels") != new.get("channels"):
            raise MediaError("Output audio channel count changed.")
        old_duration, new_duration = duration(source, old), duration(metadata, new)
        if old_duration is not None and (new_duration is None or abs(old_duration - new_duration) > tolerance):
            raise MediaError("An audio track changed duration beyond tolerance.")
    color_mapping = {"color_space": "colorspace", "color_transfer": "color_trc",
                     "color_primaries": "color_primaries", "color_range": "color_range"}
    for key in color_mapping:
        original = source_video.get(key)
        expected_tag = "tv" if key == "color_range" and original == "pc" else original
        if original and original not in ("unknown", "unspecified") and video.get(key) != expected_tag:
            raise MediaError("Output failed to match {}: expected {}, got {}.".format(key, expected_tag, video.get(key)))
    for key in ("color_space", "color_transfer", "color_primaries"):
        if source_video.get(key) in (None, "unknown", "unspecified") and video.get(key) not in (None, "unknown", "unspecified"):
            raise MediaError("Output introduced an unsupported color assumption for {}: {}.".format(key, video.get(key)))
    # ProRes has no reordered B-frames. Verify actual packet spacing, not just a nominal fps tag.
    packets = command([ffprobe, "-v", "error", "-select_streams", "v:0",
                       "-show_packets", "-show_entries", "packet=pts_time,duration_time",
                       "-of", "csv=p=0", str(output)], "output cadence scan")
    times, packet_durations = [], []
    for line in packets.splitlines():
        fields = line.split(",")
        if len(fields) >= 2:
            timestamp, packet_duration = number(fields[0]), number(fields[1])
            if timestamp is not None:
                times.append(timestamp)
            if packet_duration is not None:
                packet_durations.append(packet_duration)
    if not times or not packet_durations:
        raise MediaError("Could not verify output frame timestamps.")
    expected = 1.0 / fps
    deltas = [b - a for a, b in zip(times, times[1:])]
    if any(abs(value - expected) > 0.00002 for value in deltas + packet_durations):
        raise MediaError("Output packet timing is not constant at the requested frame rate.")
    command([ffmpeg, "-hide_banner", "-v", "error", "-xerror", "-i", str(output),
             "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"], "full output decode")
    return metadata, {"full_decode": "passed", "packet_cadence": "passed",
                      "video_frame_count": len(times), "frame_duration_seconds": expected,
                      "duration_tolerance_seconds": tolerance,
                      "audio_track_count": len(audio_after)}


def publish_no_replace(temporary, destination):
    # Linking on the same filesystem publishes a complete file atomically and
    # refuses an existing destination, including a racing writer. rename() alone
    # can overwrite on Unix. The temporary name is removed after publication.
    os.link(str(temporary), str(destination))
    temporary.unlink()


def convert(args, ffprobe):
    ffmpeg = executable(args.ffmpeg)
    source_path = Path(args.input).expanduser().resolve(strict=True)
    output = Path(args.output).expanduser().absolute()
    if output.suffix.lower() != ".mov":
        raise MediaError("Output must use a .mov filename.")
    sidecar = Path(str(output) + ".json")
    if os.path.lexists(str(output)) or os.path.lexists(str(sidecar)):
        raise MediaError("Output or its JSON sidecar already exists; choose a new filename.")
    if not output.parent.is_dir():
        raise MediaError("Output directory does not exist: {}".format(output.parent))
    source = probe(source_path, ffprobe)
    video = video_stream(source)
    hdr = hdr_reasons(video)
    if hdr:
        raise MediaError("HDR preparation is unsupported: {}. Use an explicit color-managed HDR/SDR workflow in Resolve.".format("; ".join(hdr)))
    if args.fps <= 0 or args.fps > 120:
        raise MediaError("--fps must be greater than zero and no higher than 120.")
    width, height = (3840, 2160) if args.orientation == "landscape" else (2160, 3840)
    fps_text = "{:g}".format(args.fps)
    range_tag = video.get("color_range")
    range_filter = ":in_range={0}:out_range={0}".format(range_tag) if range_tag in ("tv", "pc") else ""
    final_range_filter = ":in_range=pc:out_range=tv" if range_tag == "pc" else range_filter
    eight_bit_formats = {"yuvj420p", "yuvj422p", "yuvj444p", "yuvj440p", "yuv420p", "yuv422p",
                         "yuv444p", "yuv410p", "yuv411p", "yuv440p", "nv12", "nv21", "rgb24",
                         "bgr24", "argb", "rgba", "abgr", "bgra", "gbrp", "gray"}
    eight_bit_range_mapping = range_tag == "pc" and (
        video.get("bits_per_raw_sample") == "8" or video.get("pix_fmt") in eight_bit_formats)
    # FFmpeg autorotation runs before these filters. Normalize pixel shape at
    # source display resolution before interpolation/final canvas scaling.
    filters = ["scale=w='max(2,trunc(iw*sar/2)*2)':h='max(2,trunc(ih/2)*2)'" + range_filter,
               "setsar=1"]
    if args.frame_policy == "interpolate":
        original_duration = duration(source, video)
        if original_duration is None:
            raise MediaError("Interpolation needs a known source duration to avoid truncating its final frames.")
        source_fps = rate(video.get("avg_frame_rate")) or rate(video.get("r_frame_rate")) or 1.0
        # minterpolate buffers future frames and otherwise drops the clip tail.
        # Supply cloned tail frames, then trim to the original video duration.
        filters.append("tpad=stop_mode=clone:stop_duration={:.9f}".format(max(0.25, 3.0 / source_fps)))
        filters.append("minterpolate=fps={}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1".format(fps_text))
        filters.append("trim=duration={:.9f}".format(original_duration))
    else:
        filters.append("fps=fps={}:round=near".format(fps_text))
    direction = "decrease" if args.fit == "contain" else "increase"
    filters.append("scale=w={}:h={}:force_original_aspect_ratio={}:force_divisible_by=2:flags=lanczos{}".format(width, height, direction, final_range_filter))
    if eight_bit_range_mapping:
        # swscale's direct 8-bit full -> 10-bit limited conversion maps 255 to
        # 937 instead of 940. Map range at the source bit depth before the
        # encoder's 10-bit conversion, preserving black/white endpoints.
        filters.append("format=yuv422p")
    if args.fit == "contain":
        filters.append("pad={}:{}:(ow-iw)/2:(oh-ih)/2:color=black".format(width, height))
    else:
        filters.append("crop={}:{}:(iw-ow)/2:(ih-oh)/2".format(width, height))
    filters.append("setsar=1")
    temporary = None
    json_temporary = None
    published = []
    try:
        handle, temporary_name = tempfile.mkstemp(prefix=".{}-".format(output.stem), suffix=".mov", dir=str(output.parent))
        os.close(handle)
        temporary = Path(temporary_name)
        invocation = [ffmpeg, "-hide_banner", "-v", "error", "-nostdin", "-y",
                      "-autorotate", "-i", str(source_path), "-map", "0:{}".format(video["index"]),
                      "-map", "0:a?", "-map_metadata", "0", "-vf", ",".join(filters),
                      "-c:v", "prores_ks", "-profile:v", "3" if args.profile == "hq" else "2",
                      "-pix_fmt", "yuv422p10le", "-fps_mode", "cfr", "-r", fps_text,
                      "-metadata:s:v:0", "rotate=0"]
        for source_key, option in (("color_space", "-colorspace"), ("color_transfer", "-color_trc"),
                                   ("color_primaries", "-color_primaries"), ("color_range", "-color_range")):
            tag = "tv" if source_key == "color_range" and range_tag == "pc" else video.get(source_key, "unknown")
            invocation.extend([option, tag])
        invocation.extend(["-c:a", "pcm_s16le", "-ar", "48000", "-movflags", "+faststart", str(temporary)])
        print("Preparing {}x{} at {} fps; {}; {}. Source remains unchanged.".format(
            width, height, fps_text, "repeat/drop cadence" if args.frame_policy == "cadence" else "optical-flow interpolation",
            "retain full picture with padding" if args.fit == "contain" else "crop to fill"), file=sys.stderr)
        command(invocation, "ffmpeg preparation")
        output_metadata, validation = validate(temporary, source, width, height, args.fps, ffmpeg, ffprobe)
        output_description = describe(output, output_metadata)
        output_description["source_cadence"]["variable_frame_rate"] = False
        output_description["source_cadence"]["note"] = "Constant output cadence verified from packet timestamps."
        record = {
            "schema_version": 1,
            "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "input": describe(source_path, source),
            "operation": {
                "orientation": args.orientation, "canvas_width": width, "canvas_height": height,
                "fps": args.fps, "frame_policy": args.frame_policy, "fit": args.fit,
                "profile": "ProRes 422 HQ" if args.profile == "hq" else "ProRes 422",
                "scaling": "Lanczos pixel resampling; no AI super-resolution and no recovered source detail",
                "frame_policy_note": "Frames are repeated/dropped; no interpolated motion" if args.frame_policy == "cadence" else "FFmpeg optical flow at source display resolution; cloned tail frames prevent filter truncation and output is trimmed to source duration; may introduce artifacts; no AI super-resolution",
                "color_note": "Source primaries/transfer/matrix retained when provided; missing values remain unspecified; full-range pc is explicitly mapped to ProRes video-range tv; no HDR tone mapping",
                "color_range_conversion": {"from": "pc", "to": "tv", "method": "FFmpeg final scale in_range=pc:out_range=tv; pixel-level full-to-video range mapping", "eight_bit_mapping_before_ten_bit_encode": eight_bit_range_mapping} if range_tag == "pc" else None,
                "audio": "All source audio tracks retained as PCM s16le, 48 kHz; original channel counts",
                "autorotation": True,
                "omitted_video_streams": [s["index"] for s in source.get("streams", [])
                                          if s.get("codec_type") == "video" and s["index"] != video["index"]],
                "ffmpeg_command": invocation[:-1] + [str(output)]
            },
            "output": output_description, "validation": validation,
            "limitations": ["4K is an output canvas, not proof of native 4K source detail.",
                            "30 fps is a delivery cadence, not proof of 30 unique source frames per second.",
                            "Only the first non-cover video stream is prepared; all audio tracks are retained.",
                            "HDR/PQ/HLG or HDR metadata is rejected; untagged footage needs color identification in Resolve.",
                            "Full-range (pc) pixels are explicitly mapped to video-range (tv) for ProRes; primaries/transfer/matrix are not guessed."]
        }
        handle, json_name = tempfile.mkstemp(prefix=".{}-".format(output.stem), suffix=".json", dir=str(output.parent))
        json_temporary = Path(json_name)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        publish_no_replace(temporary, output)
        published.append(output)
        publish_no_replace(json_temporary, sidecar)
        published.append(sidecar)
        print(json.dumps({"output": str(output), "sidecar": str(sidecar), "validation": validation}, ensure_ascii=False, indent=2))
    except BaseException:
        # Published paths here belong to this invocation; no existing file was
        # replaced. If sidecar publication fails, roll back our new MOV as well.
        for path in published:
            path.unlink(missing_ok=True)
        raise
    finally:
        for path in (temporary, json_temporary):
            if path is not None:
                path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg executable path (or PATH name)")
    parser.add_argument("--ffprobe", default="ffprobe", help="ffprobe executable path (or PATH name)")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect_parser = commands.add_parser("inspect", help="Print source metadata and honest cadence/HDR notes as JSON")
    inspect_parser.add_argument("--ffmpeg", default=argparse.SUPPRESS)
    inspect_parser.add_argument("--ffprobe", default=argparse.SUPPRESS)
    inspect_parser.add_argument("input")
    convert_parser = commands.add_parser("convert", help="Prepare a new 4K ProRes MOV plus OUTPUT.mov.json")
    convert_parser.add_argument("--ffmpeg", default=argparse.SUPPRESS)
    convert_parser.add_argument("--ffprobe", default=argparse.SUPPRESS)
    convert_parser.add_argument("input")
    convert_parser.add_argument("output")
    convert_parser.add_argument("--orientation", choices=("landscape", "portrait"), default="landscape")
    convert_parser.add_argument("--fps", type=float, default=30)
    convert_parser.add_argument("--frame-policy", choices=("cadence", "interpolate"), default="cadence")
    convert_parser.add_argument("--fit", choices=("contain", "cover"), default="contain")
    convert_parser.add_argument("--profile", choices=("422", "hq"), default="422")
    args = parser.parse_args()
    try:
        ffprobe = executable(args.ffprobe)
        if args.command == "inspect":
            path = Path(args.input).expanduser().resolve(strict=True)
            print(json.dumps(describe(path, probe(path, ffprobe)), ensure_ascii=False, indent=2))
        else:
            convert(args, ffprobe)
    except (MediaError, OSError, ValueError) as error:
        print("Error: {}".format(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; temporary files cleaned up.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
