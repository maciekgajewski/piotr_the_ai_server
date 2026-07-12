# The "Piotr" project

A project to turn this computer into a AI server

## Project goals

- Solid, stable server, with logs, monitoring, resiliency
- Uses local models on local GPU as much as possible
- Supports multiple satellites of various types
- Capable of multiple actions using dynamic, extensible tool system
- Works in simple wakeword-utterance-reply mode, but can also switch to conversational mode
- Recognize users by voice

### Naming

- "Piotr" is the project and computer name
- The AI assistant is called "Ryszard"

## Architecture decisions

- STT should become an event source that can emit partial and final transcript events per microphone session.

## Directory

- notes/ - notes from various endeavors
- firmware/esphome/ - ESP32-S3-BOX-3 satellite firmware templates
- docs/ - normative architecture, task plans, and operational guides; start with [the documentation index](docs/README.md)

## ESP32-S3-BOX-3 satellite

The first satellite target is an ESP32-S3-BOX-3 connected over 2.4 GHz Wi-Fi.
See [docs/esp32-s3-box-3-satellite.md](docs/esp32-s3-box-3-satellite.md).
