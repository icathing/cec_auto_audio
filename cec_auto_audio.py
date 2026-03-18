#!/usr/bin/env python3
"""
cec_auto_audio.py

Goal:
- Keep cec-client mostly passive: just watch bus traffic.
- When a console (any Playback device) becomes Active Source
  and Denon does NOT quickly issue Set System Audio Mode,
  send a single System Audio Mode Request:
      tx 15:70:00:00

Assumptions matching your setup:
- TV: logical 0x0
- Denon AVR (ARC): logical 0x5 (Audio device)
- libCEC client: logical 0x1 (Recorder 1)
- Consoles: Playback LAs (0x4, 0x8, 0xB in practice)
"""

import subprocess
import sys
import time
import re

# ---- CONFIG -------------------------------------------------------------

# Treat these logical addresses as "playback devices" (consoles + Apple TV).
CONSOLE_LAS = {0x4, 0x8, 0xB}

DENON_LA = 0x5

# Minimum gap between our own injections, to avoid spam.
MIN_INJECTION_INTERVAL_SEC = 3.0

# Start in dry-run so you can verify behavior without actually sending.
DRY_RUN = True

# ------------------------------------------------------------------------


FRAME_RE = re.compile(
    r'>>\s+([0-9A-Fa-f]{2}):'      # header byte (source/dest LAs)
    r'([0-9A-Fa-f]{2})'            # opcode
    r'(?:[:]([0-9A-Fa-f:]+))?'     # optional data bytes
)


def now_str() -> str:
    return time.strftime("%H:%M:%S")


def parse_frame(line):
    """
    Parse a TRAFFIC line like:
      TRAFFIC: [   37491]     >> bf:82:36:00

    Returns (src_la, dst_la, opcode, data_bytes) or None if not a frame.
    """
    m = FRAME_RE.search(line)
    if not m:
        return None

    header = int(m.group(1), 16)
    opcode = int(m.group(2), 16)
    data_raw = m.group(3)

    src_la = header >> 4
    dst_la = header & 0xF

    data = []
    if data_raw:
        data = [int(b, 16) for b in data_raw.split(":") if b]

    return src_la, dst_la, opcode, data


def main():
    print("[INFO] Starting CEC auto-audio helper.")
    print(f"[INFO] DRY_RUN = {DRY_RUN}")
    print("[INFO] Strategy:")
    print("  - Watch for Active Source (opcode 0x82) from any Playback LA.")
    print("  - Give Samsung/Denon a moment to do their own 5f:72:01.")
    print("  - If they don't, send: tx 15:70:00:00 (System Audio Mode Request).")
    print()

    # One interactive cec-client; we both read and write to it.
    # -d 8 = TRAFFIC only, which keeps output manageable.
    # (You can also bump this to 31 while debugging.)
    cmd = ["cec-client", "-d", "8"]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
    )

    if proc.stdin is None or proc.stdout is None:
        print("[ERROR] Failed to open cec-client pipes.")
        sys.exit(1)

    # State: pending console that just became active, waiting to see if Denon
    # responds on its own with 5f:72:01.
    on_status = None  # dict with keys time, count
    last_injection_time = 0.0

    print(f"[INFO] Spawned: {' '.join(cmd)}")
    print("[INFO] Watching CEC traffic...\n")

    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            # Always show raw TRAFFIC lines so you can correlate later if needed.
            print(line)

            frame = parse_frame(line)
            if not frame:
                # Non-TRAFFIC lines / noise; still keep scanning.
                continue

            src_la, dst_la, opcode, data = frame

            # 1) Denon Set System Audio Mode (5f:72:01)
            if (
                src_la == DENON_LA
                and dst_la == 0xF
                and opcode == 0x72
                and data and data[0] == 0x01
            ):

                ts = now_str()
                # if we have seen this within the last minute, we could be in a contention situation
                # if more than a minute, we should be ok, so should reset
                if on_status is not None:
                    if on_status["time"] < (time.time()-60):
                        # reset
                        on_status["time"] = time.time()
                        on_status["count"] = 1

                    else:
                        # update seen time and count
                        on_status["time"] = time.time()
                        on_status["on_count"] = on_status.get("on_count", 0) + 1

                else:
                    on_status["time"] = time.time()
                    on_status["count"] = 1

                print(f"[AUTO {ts}] Detected Denon Set System Audio Mode (5f:72:01). Count in last minute is {on_status["count"]}.")

            if (
                src_la == DENON_LA
                and dst_la == 0xF
                and opcode == 0x72
                and data and data[0] == 0x00
            ):
                # Denon set off status, should not do this
                now = time.time()
                if on_status is None:
                    print("Hm, saw denon do audio mode off, but haven't seen on. Unexpected, will do nothing, but you should investigate")
                elif on_status is not None:
                    if now < (on_status["time"] + 60):
                        # do something...
                        # Rate limiting: don't spam if something weird happens.
                        if now - last_injection_time < MIN_INJECTION_INTERVAL_SEC:
                            print("Saw denon do audio mode off, but have already tried to correct recently, doing nothing.")
                            
                        else:
                            cmd_str = "tx 5f:72:01"
                            if DRY_RUN:
                                print(
                                    f"[AUTO {ts}] [DRY RUN] Would send: {cmd_str} "
                                    f"(System Audio Mode Request to Denon for TV)."
                                )
                            else:
                                print(
                                    f"[AUTO {ts}] Sending: {cmd_str} "
                                    f"(System Audio Mode Request to Denon for TV)."
                                )
                                try:
                                    proc.stdin.write(cmd_str + "\n")
                                    proc.stdin.flush()
                                    last_injection_time = now
                                except BrokenPipeError:
                                    print(
                                        f"[ERROR {ts}] Failed to write '{cmd_str}' to cec-client "
                                        "(BrokenPipeError)."
                                    )
                                    # No point continuing if we can't send.
                                    break

                    else:
                        time_elapsed = now - on_status["time"]
                        print(f"Saw denon set audio mode off, but it was {time_elapsed} seconds ago, above threshold. Maybe tune settings if you want something done.")


            # If there's no pending console or no timeout yet, just keep looping.

    except KeyboardInterrupt:
        print("\n[INFO] Interrupted by user, shutting down...")

    finally:
        try:
            proc.stdin.write("q\n")
            proc.stdin.flush()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
        print("[INFO] cec-client terminated.")


if __name__ == "__main__":
    main()
