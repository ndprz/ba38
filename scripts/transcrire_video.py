#!/usr/bin/env python3
"""
Transcrit l'audio d'une vidéo en sous-titres .vtt via faster-whisper (local, hors-ligne).
Exécuté en subprocess isolé depuis ba38_evenements.py pour ne pas garder le modèle
Whisper chargé en mémoire dans les workers gunicorn.

Usage: transcrire_video.py <video_path> <vtt_path> [model_size]
"""
import sys

MODEL_SIZE_DEFAUT = "small"


def format_timestamp(seconds):
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def main():
    if len(sys.argv) < 3:
        print("Usage: transcrire_video.py <video_path> <vtt_path> [model_size]", file=sys.stderr)
        sys.exit(2)

    video_path = sys.argv[1]
    vtt_path = sys.argv[2]
    model_size = sys.argv[3] if len(sys.argv) > 3 else MODEL_SIZE_DEFAUT

    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _info = model.transcribe(video_path, language="fr", vad_filter=True)

    with open(vtt_path, "w", encoding="utf-8") as out:
        out.write("WEBVTT\n\n")
        for segment in segments:
            texte = segment.text.strip()
            if not texte:
                continue
            out.write(f"{format_timestamp(segment.start)} --> {format_timestamp(segment.end)}\n")
            out.write(f"{texte}\n\n")

    print(vtt_path)


if __name__ == "__main__":
    main()
